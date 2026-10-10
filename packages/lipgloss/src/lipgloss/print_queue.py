import asyncio

from collections import deque

from lipgloss import printer
from lipgloss.printer import LabelError, PrinterUnavailable

MAX_COPIES = 100


class PrintJob:
    def __init__(self, job_id, description, pages, reply_to=None, source="unknown", announce_when_done=False):
        self.job_id = job_id
        self.description = description
        self.pages = pages  # Rendered image paths, printed in order
        self.printed = 0
        # Opaque token naming the submitter, echoed on every event about this
        # job so a client can pick its own updates off the shared stream
        self.reply_to = reply_to
        self.source = source
        self.announce_when_done = announce_when_done

    @property
    def total(self):
        return len(self.pages)

    @property
    def remaining(self):
        return self.total - self.printed

    @property
    def next_page(self):
        return self.pages[self.printed]

    def summary(self):
        if self.total > 1:
            return f"{self.description} ({self.remaining}/{self.total} labels left)"

        return self.description


class PrintQueue:
    """Prints one label at a time in the background.

    The printer is checked before every single label, and any printer problem
    (out of labels, lid open, asleep, unplugged) pauses the whole queue instead
    of burning through the rest of the jobs failing. Nothing is lost: the job
    that failed stays at the front of the queue and picks up where it left off
    once someone resumes it.
    """

    def __init__(self, port, model, delay=1, log=None, events=None):
        self._events = events
        self._port = port
        self._model = model
        self._delay = delay  # niimbot cant instantly take a new job, so we give it extra time between each one
        self._log = log or print

        self._jobs = deque()
        self._wake = asyncio.Event()
        # Only one thing can hold the serial port at a time, so every trip to the
        # printer, queued or not, goes through here
        self._printer_lock = asyncio.Lock()
        self._paused = False
        self._pause_reason = None
        self._next_job_id = 1
        self._task = None

    # Queue state

    @property
    def paused(self):
        return self._paused

    @property
    def pause_reason(self):
        return self._pause_reason

    @property
    def pending_jobs(self):
        return len(self._jobs)

    @property
    def pending_labels(self):
        return sum(job.remaining for job in self._jobs)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker())

        return self._task

    async def stop(self):
        if self._task is None:
            return

        self._task.cancel()

        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

        self._task = None

    # Adding work

    def add(self, pages, description, copies=1, reply_to=None, source="unknown"):
        """Queue rendered label images. Returns (job, message)."""
        if isinstance(pages, str):
            pages = [pages]

        if copies < 1:
            return None, "Quantity must be at least 1"

        if copies > MAX_COPIES:
            return None, f"Quantity too high, {MAX_COPIES} is the limit per job"

        pages = list(pages) * copies

        if not pages:
            return None, "Nothing to print"

        try:
            for page in dict.fromkeys(pages):
                printer.check_label(page, self._model)
        except LabelError as e:
            return None, f"Unable to print, {e}"

        job = PrintJob(
            job_id=self._next_job_id,
            description=description,
            pages=pages,
            reply_to=reply_to,
            source=source,
            announce_when_done=len(pages) > 1 or len(self._jobs) > 0,
        )

        self._next_job_id += 1
        self._jobs.append(job)

        if not self._paused:
            self._wake.set()

        return job, self._queued_message(job)

    def _queued_message(self, job):
        if job.total > 1:
            queued = f"Queued job {job.job_id}: {job.total} labels, {job.description}"
        else:
            queued = f"Queued job {job.job_id}: {job.description}"

        if self._paused:
            return (
                f"{queued}\n"
                f"The print queue is paused: {self._pause_reason}\n"
                f"Nothing will print until the printer is fixed and the queue is resumed."
            )

        ahead = self.pending_labels - job.remaining

        if ahead > 0:
            return f"{queued}\n{ahead} label(s) ahead of it in the queue."

        return f"{queued}\nPrinting...\nif this is the first print after returning from sleep it may be blank."

    # Talking to the printer

    async def printer_media(self):
        """Media info for the loaded roll, raises PrinterUnavailable if it cant print."""
        async with self._printer_lock:
            return await asyncio.to_thread(printer.check_printer, self._port)

    async def printer_info(self):
        async with self._printer_lock:
            return await asyncio.to_thread(printer.niimbot_printer_info, self._port)

    # Managing the queue

    def cancel(self, job_id):
        """(cancelled, message).

        Whether it caught the job in time is a separate answer from the text:
        a client offering a cancel button has to tell "stopped it" from "it had
        already printed", and sniffing the message for which is not an answer.
        """
        for job in self._jobs:
            if job.job_id == job_id:
                self._jobs.remove(job)
                return True, f"Cancelled job {job_id}: {job.description}"

        return False, f"No queued job with id {job_id}"

    def clear(self):
        cleared_jobs = len(self._jobs)
        cleared_labels = self.pending_labels

        self._jobs.clear()

        if cleared_jobs == 0:
            return "The print queue is already empty"

        return f"Cleared {cleared_jobs} job(s), {cleared_labels} label(s) never printed"

    async def resume(self):
        if not self._paused:
            if not self._jobs:
                return "The print queue is not paused, and there is nothing waiting to print"

            return "The print queue is not paused"

        try:
            media_info = await self.printer_media()
        except PrinterUnavailable as e:
            self._pause_reason = str(e)
            return f"Still unable to print, {e}\nThe print queue is staying paused."

        remaining_media = printer.media_remaining(media_info)

        self._paused = False
        self._pause_reason = None

        if self._jobs:
            self._wake.set()
            return f"Print queue resumed, {self.pending_labels} label(s) to print, {remaining_media} left on the roll"

        return f"Print queue resumed, nothing waiting to print, {remaining_media} labels left on the roll"

    def _status_parts(self):
        """Title, description and one row per job, shared by the text and embed versions."""
        if self._paused:
            title = "Print Queue: Paused"
        elif self._jobs:
            title = "Print Queue: Printing"
        else:
            title = "Print Queue: Idle"

        description = []

        if self._paused:
            description.append(f"{self._pause_reason}\nResume the queue once the printer is sorted out.")

        if self._jobs:
            description.append(f"{len(self._jobs)} job(s), {self.pending_labels} label(s) left")
        else:
            description.append("Nothing waiting to print.")

        rows = []

        for index, job in enumerate(self._jobs):
            if index == 0:
                state = "Paused" if self._paused else "Printing"
            else:
                state = "Waiting"

            rows.append(
                {
                    "HEADER": f"Job {job.job_id} ({state})",
                    "JOB_ID": str(job.job_id),
                    "DESCRIPTION": job.description,
                    "LABELS": f"{job.remaining}/{job.total}",
                    "SOURCE": job.source,
                    "STATE": state,
                }
            )

        return title, "\n".join(description), rows

    def status(self):
        """The queue as plain data. Callers decide whether it becomes a table or an embed."""
        title, description, rows = self._status_parts()

        return {
            "title": title,
            "description": description,
            "paused": self._paused,
            "pause_reason": self._pause_reason,
            "pending_jobs": self.pending_jobs,
            "pending_labels": self.pending_labels,
            "jobs": rows,
        }

    # Printing

    def _publish(self, job, event, title, message, urgent=False):
        if self._events is None:
            return

        self._events.publish(
            {
                "event": event,
                "job_id": job.job_id,
                "description": job.description,
                "reply_to": job.reply_to,
                "source": job.source,
                "title": title,
                "message": message,
                "urgent": urgent,
            }
        )

    def _finish(self, job):
        """Drop a finished job, by identity rather than position.

        The worker holds a reference across an await, and a cancel during that
        await can have already taken the job out of the queue. A blind popleft
        would then throw away whichever job moved up into its place.
        """
        if self._jobs and self._jobs[0] is job:
            self._jobs.popleft()

    def _pause(self, reason):
        self._paused = True
        self._pause_reason = reason
        self._wake.clear()

    async def _worker(self):
        while True:
            await self._wake.wait()

            if self._paused or not self._jobs:
                self._wake.clear()
                continue

            job = self._jobs[0]
            page = job.next_page

            try:
                async with self._printer_lock:
                    await asyncio.to_thread(printer.niimbot_print, page, self._port, self._model)
            except PrinterUnavailable as e:
                self._pause(str(e))

                self._log(f"Print queue paused, {e}")

                paused_message = (
                    f"Unable to print, {e}\n"
                    f"The print queue is paused with {self.pending_labels} label(s) left "
                    f"(job {job.job_id}: {job.summary()}).\n"
                    f"Fix the printer, then resume the queue."
                )

                self._publish(job, "printer.fault", "Print Queue Paused", paused_message, urgent=True)

                continue
            except LabelError as e:
                # The printer is fine, this label just cant be printed, so skip it and carry on
                job.pages.pop(job.printed)

                if job.remaining <= 0:
                    self._finish(job)

                self._log(f"Job {job.job_id} skipped a label, {e}")

                skipped_message = f"Skipped a label in job {job.job_id} ({job.description}), {e}"

                self._publish(job, "job.skipped", "Label Skipped", skipped_message, urgent=True)

                continue

            job.printed += 1

            if job.remaining <= 0:
                self._finish(job)

                self._log(f"Job {job.job_id} finished, {job.description}")

                if job.announce_when_done:
                    finished_message = f"Finished printing job {job.job_id}: {job.total} label(s), {job.description}"

                    self._publish(job, "job.done", "Print Finished", finished_message)

            await asyncio.sleep(self._delay)
