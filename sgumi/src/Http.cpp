#include "Http.hpp"

#include <curl/curl.h>

namespace http {
namespace {

// Long enough that a busy Niimbot driver does not read as "down", short enough
// that the UI is not stuck on a stale snapshot for a whole poll cycle.
constexpr long kTimeoutSeconds = 5;

// An upload gets longer than a read does: the body is a PNG rather than a line
// of JSON, and the far end writes it to disk before answering.
constexpr long kUploadTimeoutSeconds = 30;

size_t writeToString(char* data, size_t size, size_t count, void* userp) {
    const size_t total = size * count;
    static_cast<std::string*>(userp)->append(data, total);
    return total;
}

// Everything the verbs share. A fresh handle per call: these happen at most
// once a second on a worker thread, so handle reuse would buy nothing
// measurable and cost the rule that nothing here is shared between threads.
//
// verb is nullptr for the two libcurl already has a flag for, GET and POST;
// anything else is spelled out with CUSTOMREQUEST.
Response perform(
    const std::string& url,
    const std::string& token,
    const std::string* body,
    const char* verb = nullptr)
{
    Response response;

    CURL* curl = curl_easy_init();

    if (!curl) {
        response.error = "curl_easy_init failed";
        return response;
    }

    curl_slist* headers = nullptr;

    if (!token.empty()) {
        // Every endpoint except lipgloss's /health and claws's /health checks
        // a bearer token; sending it to those two as well is harmless.
        const std::string authorization = "Authorization: Bearer " + token;
        headers = curl_slist_append(headers, authorization.c_str());
    }

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeToString);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response.body);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, kTimeoutSeconds);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, kTimeoutSeconds);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);

    if (body) {
        headers = curl_slist_append(headers, "Content-Type: application/json");

        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body->c_str());

        // Explicit length: a body is not guaranteed to be free of embedded
        // nulls, and libcurl would otherwise strlen it.
        curl_easy_setopt(
            curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(body->size()));
    }

    if (verb) {
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, verb);
    }

    if (headers) {
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    }

    const CURLcode result = curl_easy_perform(curl);

    if (result == CURLE_OK) {
        response.transportOk = true;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &response.status);
    } else {
        response.error = curl_easy_strerror(result);
    }

    if (headers) {
        curl_slist_free_all(headers);
    }

    curl_easy_cleanup(curl);
    return response;
}

}  // namespace

void globalInit() {
    curl_global_init(CURL_GLOBAL_DEFAULT);
}

void globalCleanup() {
    curl_global_cleanup();
}

Response get(const std::string& url, const std::string& token) {
    return perform(url, token, nullptr);
}

Response post(
    const std::string& url,
    const std::string& token,
    const std::string& body)
{
    return perform(url, token, &body);
}

Response del(const std::string& url, const std::string& token) {
    return perform(url, token, nullptr, "DELETE");
}

Response postForm(
    const std::string& url,
    const std::string& token,
    const std::vector<FormField>& fields)
{
    Response response;

    CURL* curl = curl_easy_init();

    if (!curl) {
        response.error = "curl_easy_init failed";
        return response;
    }

    curl_slist* headers = nullptr;

    if (!token.empty()) {
        const std::string authorization = "Authorization: Bearer " + token;
        headers = curl_slist_append(headers, authorization.c_str());
    }

    // Deliberately not setting Content-Type: curl_mime generates the boundary
    // and the header to match it, and overriding that produces a body no
    // multipart parser can read.
    curl_mime* form = curl_mime_init(curl);

    for (const FormField& field : fields) {
        curl_mimepart* part = curl_mime_addpart(form);

        curl_mime_name(part, field.name.c_str());
        curl_mime_data(part, field.value.data(), field.value.size());

        if (!field.filename.empty()) {
            curl_mime_filename(part, field.filename.c_str());
        }

        if (!field.contentType.empty()) {
            curl_mime_type(part, field.contentType.c_str());
        }
    }

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_MIMEPOST, form);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeToString);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response.body);

    // A label's worth of PNG is tens of kilobytes, but it is still an upload
    // over a tailnet, so this gets more room than a poll does.
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, kUploadTimeoutSeconds);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, kTimeoutSeconds);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);

    if (headers) {
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    }

    const CURLcode result = curl_easy_perform(curl);

    if (result == CURLE_OK) {
        response.transportOk = true;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &response.status);
    } else {
        response.error = curl_easy_strerror(result);
    }

    // Freed before the handle, which is what the curl_mime docs ask for.
    curl_mime_free(form);

    if (headers) {
        curl_slist_free_all(headers);
    }

    curl_easy_cleanup(curl);
    return response;
}

namespace {

// What the three C callbacks below are handed through their userdata pointer.
struct StreamContext {
    CURL* curl = nullptr;
    const std::function<void(long)>* onOpen = nullptr;
    const std::function<void(const char*, size_t)>* onChunk = nullptr;
    const std::function<bool()>* keepGoing = nullptr;
    bool opened = false;
};

// Called once per header line, and once more with the blank line that ends
// them. That blank line is the only reliable "the response has started" signal
// libcurl offers from inside a transfer that will not finish.
size_t onHeader(char* data, size_t size, size_t count, void* userp) {
    auto* context = static_cast<StreamContext*>(userp);
    const size_t total = size * count;

    const std::string line(data, total);
    const bool blank = line == "\r\n" || line == "\n";

    if (blank && !context->opened) {
        long status = 0;
        curl_easy_getinfo(context->curl, CURLINFO_RESPONSE_CODE, &status);

        context->opened = true;
        (*context->onOpen)(status);
    }

    return total;
}

size_t onBody(char* data, size_t size, size_t count, void* userp) {
    auto* context = static_cast<StreamContext*>(userp);
    const size_t total = size * count;

    // Returning anything but the full length aborts the transfer, which is the
    // documented way to stop one from inside the write callback.
    if (!(*context->keepGoing)()) {
        return 0;
    }

    (*context->onChunk)(data, total);
    return total;
}

int onProgress(void* userp, curl_off_t, curl_off_t, curl_off_t, curl_off_t) {
    auto* context = static_cast<StreamContext*>(userp);
    return (*context->keepGoing)() ? 0 : 1;
}

}  // namespace

Response stream(
    const std::string& url,
    const std::string& token,
    const std::function<void(long)>& onOpen,
    const std::function<void(const char*, size_t)>& onChunk,
    const std::function<bool()>& keepGoing)
{
    Response response;

    CURL* curl = curl_easy_init();

    if (!curl) {
        response.error = "curl_easy_init failed";
        return response;
    }

    curl_slist* headers = nullptr;

    if (!token.empty()) {
        const std::string authorization = "Authorization: Bearer " + token;
        headers = curl_slist_append(headers, authorization.c_str());
    }

    headers = curl_slist_append(headers, "Accept: text/event-stream");

    StreamContext context;
    context.curl = curl;
    context.onOpen = &onOpen;
    context.onChunk = &onChunk;
    context.keepGoing = &keepGoing;

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, onHeader);
    curl_easy_setopt(curl, CURLOPT_HEADERDATA, &context);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, onBody);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &context);

    // Deliberately no CURLOPT_TIMEOUT -- see the header. The connect phase
    // still gets one, so a host that is not there fails as fast as a poll does.
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, kTimeoutSeconds);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);

    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, onProgress);
    curl_easy_setopt(curl, CURLOPT_XFERINFODATA, &context);

    const CURLcode result = curl_easy_perform(curl);

    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &response.status);

    // Both abort codes are this side stopping a stream that was working, so
    // they are not transport failures -- the caller asked for them.
    if (result == CURLE_OK ||
        result == CURLE_ABORTED_BY_CALLBACK ||
        result == CURLE_WRITE_ERROR) {
        response.transportOk = true;
    } else {
        response.error = curl_easy_strerror(result);
    }

    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    return response;
}

std::string join(const std::string& baseUrl, const char* path) {
    std::string base = baseUrl;

    while (!base.empty() && base.back() == '/') {
        base.pop_back();
    }

    return base + path;
}

}  // namespace http
