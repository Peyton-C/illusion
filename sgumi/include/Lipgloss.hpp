#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

// Client for the lipgloss print service.
//
// Mirrors LipglossClient in packages/illusion-core/src/illusion_core/clients.py,
// which is the authority on the wire format, when an endpoint changes there,
// it changes here. Everything but POST /print/image is reached.
//
// Nothing here is on a hot path. lipgloss is a USB label printer on the far
// end, so the whole point of putting it on a worker thread is that the frame
// loop never waits on a socket.
//
// Two threads, not one. The poll thread owns GET /health and GET /queue, which
// between them describe everything the UI draws. The event thread holds GET
// /events open and rings a bell when anything lands, because lipgloss publishes
// only three things. a printer fault, a skipped label, a finished job. and
// none of them carry queue state. So the stream is a doorbell and /queue stays
// the source of truth; the stream is what lets the poll be slow without a fault
// sitting unnoticed for the length of an interval.

namespace lipgloss {

// One row of GET /queue's "jobs" array. The keys are upper-case on the wire
// because the same rows feed illusion's terminal table renderer.
struct Job {
    std::string jobId;
    std::string description;
    std::string labels;  // "remaining/total", pre-formatted by the service
    std::string source;
    std::string state;   // "Printing", "Paused" or "Waiting"
};

// Everything the UI draws, as of one poll. Copied out under a lock rather than
// read field by field, so a frame can never show half of one poll and half of
// the next.
struct Snapshot {
    // False until the first poll completes, and after any poll that failed.
    bool reachable = false;

    // Why the last attempt failed, for the status line. Empty when reachable.
    std::string error;

    // Set when the transport worked but the service refused us -- a 401 from a
    // token mismatch is the overwhelmingly likely cause and is worth saying
    // plainly, because it looks identical to "down" in every other respect.
    bool unauthorized = false;

    // GET /health. Unauthenticated on purpose over there, so these can be
    // populated even while the token is wrong -- which is exactly what makes
    // the distinction above visible.
    std::string version;
    std::string printerPort;
    std::string model;
    long long uptimeMs = 0;

    // GET /queue.
    std::string title;        // "Print Queue: Idle" and friends
    std::string description;  // may contain newlines
    bool paused = false;
    std::string pauseReason;
    int pendingJobs = 0;
    int pendingLabels = 0;
    std::vector<Job> jobs;

    // When this snapshot was taken, for the "last updated" line. Default-
    // constructed means "never polled".
    std::chrono::steady_clock::time_point polled {};
};

// lipgloss rejects anything above this with a 422 (MAX_COPIES in
// packages/lipgloss/src/lipgloss/print_queue.py). Mirrored so the UI can clamp
// rather than let a request be refused after the fact.
constexpr int kMaxCopies = 100;

// What POST /print takes. The style must already be resolved -- lipgloss knows
// label_1_line and label_2_line, not the "label" the user picked; see
// styles::resolve in main.cpp for where that happens.
struct PrintRequest {
    std::string style;
    std::string sku;
    std::string line1;
    std::string line2;
    int copies = 1;
};

// What POST /print/image takes, once the picked file has been turned into
// something the printer will accept. Not a PrintRequest: there is no style and
// no SKU, the label is the image.
struct ImageRequest {
    // PNG bytes. lipgloss writes these to a .png and hands the path to the
    // printer without looking at them, so whatever is here is what prints.
    std::string png;

    // Shown in the queue's Description column. lipgloss truncates at 60.
    std::string description;

    int copies = 1;
};

// lipgloss truncates description to this in print_image. Mirrored so the box
// can stop at the same place rather than silently losing the end.
constexpr int kMaxImageDescription = 60;

// The outcome of the last submitted print. One value rather than a list: the
// UI submits one at a time and only reports the most recent.
struct ActionResult {
    enum class State {
        Idle,
        Pending,
        Ok,
        Failed,
    };

    // What the result describes. One slot serves every action, so without this
    // the UI cannot word the outcome: a resume has no job id, and reporting it
    // as "Job -1 queued" would be nonsense.
    enum class Kind {
        Print,
        Barcodes,
        Image,
        Resume,
        Clear,
        Cancel,
    };

    State state = State::Idle;
    Kind kind = Kind::Print;

    // lipgloss's own wording where it gave any -- it explains queue state
    // better than anything invented here would.
    std::string message;

    long long jobId = -1;

    // A job accepted onto a paused queue is not printing. lipgloss reports
    // this separately for exactly that reason, so "queued" is not mistaken for
    // "printed".
    bool queuePaused = false;

    // Kind::Cancel only. DELETE /queue/{id} answers 200 whether or not it
    // caught the job in time, so "it had already printed" is a successful
    // request with nothing cancelled. lipgloss returns that as its own field
    // rather than leaving it to be read out of the message, and it is the
    // difference between a warning and a failure here.
    bool cancelled = false;
};

// What the poll interval is allowed to be set to. The floor is where the old
// fixed interval sat and is what a kiosk on the same bench as the printer wants;
// the ceiling is there so a typo cannot leave the queue looking frozen.
constexpr auto kMinPollInterval = std::chrono::milliseconds(1000);
constexpr auto kMaxPollInterval = std::chrono::milliseconds(60000);
constexpr auto kDefaultPollInterval = std::chrono::milliseconds(5000);

// How long the event thread waits before dialling back in, matching the kiosk's
// lipgloss_event_loop. A drop here costs latency, never correctness, so there
// is nothing to be gained by retrying harder than that.
constexpr auto kEventRetryInterval = std::chrono::seconds(5);

// lipgloss clamps this to PREVIEW_MAX_SCALE (8) in label_maker.py. 3 is its
// default and is what the bot asks for.
constexpr int kPreviewScale = 3;

// PREVIEW_MAX_SCALE itself. An image preview picks its own magnification from
// how small the label is, so it needs the ceiling rather than the default.
constexpr int kMaxPreviewScale = 8;

// What an image preview asks to be turned by.
//
// lipgloss renders a label the way it is read and turns it a quarter
// anticlockwise on the way to the head, so the printed orientation is the read
// one rotated 90. An image is built printed-side-up, and this is the quarter
// that puts it back -- which is what makes it arrive the same shape, wide and
// short, as the preview of every rendered style.
constexpr int kPreviewRotate = 270;

// The PNG POST /preview hands back, and what went wrong if it did not.
struct PreviewResult {
    enum class State {
        Idle,
        Pending,
        Ready,
        Failed,
    };

    State state = State::Idle;

    // Raw PNG bytes. std::string rather than a vector because that is what the
    // HTTP layer fills and it holds arbitrary bytes perfectly well.
    std::string png;

    std::string error;

    // Bumped every time png is replaced. The UI uploads a texture only when
    // this changes, rather than decoding the same PNG every frame.
    unsigned long long serial = 0;
};

class Client {
public:
    Client();
    ~Client();

    Client(const Client&) = delete;
    Client& operator=(const Client&) = delete;

    // Starts the worker. Safe to call before an endpoint is set: with no URL
    // configured the worker simply reports that and sleeps.
    void start();

    // Joins the worker. Called from the shutdown path; also called by the
    // destructor, so an early return does not leak a thread.
    void stop();

    // Replaces the endpoint and wakes the worker for an immediate poll, so
    // hitting Apply in the settings window gives an answer now rather than up
    // to a poll interval later.
    void setEndpoint(std::string baseUrl, std::string token);

    // How long the poll thread waits between rounds. Anything that cannot wait
    // that long arrives on /events instead, so this is a knob for how quickly a
    // job someone else queued shows up, not for how quickly a fault does.
    //
    // Clamped to [kMinPollInterval, kMaxPollInterval]. 
    void setPollInterval(std::chrono::milliseconds interval);

    // Wakes the worker without changing anything. The Refresh button.
    void refresh();

    // Whether GET /events is currently up. Independent of the snapshot: the UI
    // can be showing a fresh poll over a dead stream, or the reverse. Nothing
    // stops printing when this is false, the UI is just as current as the poll
    // interval and no better, which is why it lives on the About page rather
    // than in the status bar.
    bool eventsConnected() const;

    // Queues a print. Returns immediately; watch actionResult() for the
    // outcome. A second call before the first finishes replaces it, which the
    // UI prevents by disabling the button while one is pending.
    void submitPrint(PrintRequest request);

    // POST /print/barcodes -- one label per SKU in [lower, upper].
    //
    // The style must be one that renders the SKU; lipgloss refuses the rest,
    // since a range of labels that do not show their SKU would be identical.
    //
    // line1/line2 are the same on every label of the run and are required
    // exactly when the style has a cell for them. line1BySku overrides them
    // per SKU, which is how a range of items that already exist each carry
    // their own name -- resolved against claws by the caller, since lipgloss
    // knows nothing about inventory.
    void submitBarcodes(int lower, int upper, std::string style,
                        std::string line1, std::string line2,
                        std::map<std::string, std::string> line1BySku);

    // POST /print/image -- a label that is a picture rather than a rendering.
    //
    // lipgloss does nothing to the image: it writes the bytes to a .png and
    // gives the printer the path. Sizing and orientation are therefore settled
    // before the request is built, not by the far end.
    void submitImage(ImageRequest request);

    // POST /queue/resume -- restarts a queue lipgloss paused because the
    // printer needed attention. Reported through actionResult() like a print,
    // since it is the same kind of "did that work?" question.
    void submitResume();

    // POST /queue/clear -- throws away every job that has not printed. There is
    // no undo on the far end, so the caller is expected to have asked first.
    void submitClear();

    // DELETE /queue/{jobId} -- pulls one job out. A job that has already
    // finished is not an error, just a cancel that arrived too late; see
    // ActionResult::cancelled.
    void submitCancel(long long jobId);

    // POST /preview -- the label this request would print, as a PNG, printing
    // nothing. Takes the same fields; copies is ignored.
    //
    // Queued separately from prints rather than sharing their slot, so asking
    // for a preview can never displace a print that was already on its way.
    void submitPreview(PrintRequest request);

    // POST /preview/image -- the same question for an image: what the print
    // head will actually lay down, which for a one bit per pixel printer means
    // a Floyd-Steinberg dither of whatever was sent.
    //
    // Answered into the same slot as submitPreview, because only one preview is
    // ever on screen and the UI does not care which endpoint drew it.
    //
    // scale is a nearest-neighbour magnification applied after the dither: a
    // label 96 pixels across is unreadable at its own size, and anything
    // smoother would average the dots back into the grey they came from.
    //
    // rotate turns the finished dither for reading rather than for printing.
    // An image is sent the way the head wants it -- across the 96, along the
    // 320 -- and kPreviewRotate turns it back into the shape every other
    // preview arrives in.
    void submitImagePreview(std::string png, int scale, int rotate);

    void clearAction();
    void clearPreview();

    // All thread-safe, all return copies, which is what lets the caller hold
    // one for a whole frame without blocking the worker.
    Snapshot snapshot() const;
    ActionResult actionResult() const;
    PreviewResult previewResult() const;

private:
    // One slot, not a queue: the UI submits one action at a time.
    struct PendingAction {
        enum class Kind {
            Print,
            Barcodes,
            Image,
            Resume,
            Clear,
            Cancel,
        };

        Kind kind = Kind::Print;
        PrintRequest print;
        int lower = 0;
        int upper = 0;
        std::map<std::string, std::string> line1BySku;

        // Kind::Image only. Held by value, so the UI is free to pick another
        // file the moment the button is pressed.
        ImageRequest image;

        // Kind::Cancel only -- the job to pull, which goes in the path.
        long long jobId = -1;
    };

    // One slot, two endpoints. A label preview describes typed fields and an
    // image preview describes bytes, but only one of them is ever being looked
    // at, so they share a result and displace each other.
    struct PendingPreview {
        enum class Kind {
            Label,
            Image,
        };

        Kind kind = Kind::Label;
        PrintRequest print;
        std::string png;
        int scale = kPreviewScale;
        int rotate = 0;
    };

    void run();
    void pollOnce();
    void runAction(const PendingAction& action);
    void runPreview(const PendingPreview& request);

    void runEvents();

    // Pulls whole lines off the front of buffer, leaving any partial tail for
    // the next chunk, and rings the bell for each one that is an event.
    void consumeEvents(std::string& buffer);

    // Asks the poll thread to go now rather than wait out its interval.
    void pollNow();

    mutable std::mutex mutex_;
    std::condition_variable wake_;

    std::string baseUrl_;
    std::string token_;
    Snapshot snapshot_;

    std::chrono::milliseconds pollInterval_ { kDefaultPollInterval };

    // Set by refresh(), by setEndpoint(), and by every event off the stream.
    // Without it a notify_all() on a thread waiting with a predicate is simply
    // re-evaluated as false and goes straight back to waiting, which is to say
    // the wake is dropped so this is what makes any of those three actually
    // shorten the wait.
    bool pollNow_ = false;

    ActionResult action_;
    std::optional<PendingAction> pendingAction_;

    PreviewResult preview_;
    std::optional<PendingPreview> pendingPreview_;

    std::atomic<bool> running_ { false };
    std::thread worker_;

    std::thread eventWorker_;
    std::atomic<bool> eventsConnected_ { false };

    // Bumped by setEndpoint. The event thread captures it when it dials, and
    // drops the connection once they differ,                     sa stream opened against the old
    // host would otherwise stay up forever, since nothing about changing a
    // setting reaches a socket that is already connected.
    std::atomic<unsigned long long> endpointGeneration_ { 0 };
};

}  // namespace lipgloss
