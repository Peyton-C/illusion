#pragma once

#include <functional>
#include <string>
#include <vector>

// The bit of libcurl both service clients need, and nothing more.

namespace http {

struct Response {
    // The request reached a server and came back. Says nothing about what the
    // server thought of it -- check status for that.
    bool transportOk = false;

    long status = 0;
    std::string body;

    // Only set when transportOk is false.
    std::string error;
};

// Called once from main, around everything else.
void globalInit();
void globalCleanup();

// All three send Authorization: Bearer <token> when token is non-empty, and
// all three block the calling thread, callers are expected to be on a worker.
Response get(const std::string& url, const std::string& token);

// Content-Type: application/json, with body sent verbatim.
Response post(
    const std::string& url,
    const std::string& token,
    const std::string& body);

// No body: the only DELETE either service has is lipgloss's /queue/{id}, which
// puts the whole request in the path.
Response del(const std::string& url, const std::string& token);

// One field of a multipart/form-data body.
struct FormField {
    std::string name;
    std::string value;

    // A non-empty filename makes this the file part, and value is then bytes
    // rather than text. FastAPI decides between UploadFile and Form on exactly
    // that, so /print/image wants its image part to carry one and its
    // description not to.
    std::string filename;
    std::string contentType;
};

// multipart/form-data, which lipgloss's /print/image needs because it takes a
// file and three form fields rather than JSON. Values are sent as bytes, so a
// PNG goes through without any encoding step.
Response postForm(
    const std::string& url,
    const std::string& token,
    const std::vector<FormField>& fields);

// A GET whose body is read as it arrives, for a response that never ends on its
// own, like lipgloss's /events. Returns when the stream stops, which for a healthy
// connection means the far end went away or keepGoing said to stop.
//
// Unlike the three above there is no total timeout, only a connect one: a
// request that is supposed to stay open all day cannot also be given five
// seconds to finish.
//
// All three callbacks run on the calling thread:
//   onOpen    once, when the response headers are in, with the HTTP status.
//             which is how a 401 on the stream is told from a dead host.
//   onChunk   with each piece of body as libcurl hands it over.
//   keepGoing asked roughly once a second even while the stream is silent, and
//             returning false aborts. That cadence is the point: lipgloss only
//             speaks every twenty seconds when idle, so waiting for a chunk to
//             notice a shutdown would stall it for that long.
Response stream(
    const std::string& url,
    const std::string& token,
    const std::function<void(long)>& onOpen,
    const std::function<void(const char*, size_t)>& onChunk,
    const std::function<bool()>& keepGoing);

// Joins a base URL and an absolute path without doubling the slash.
// "http://host:8081//health" works but looks like a bug in every log line it
// appears in.
std::string join(const std::string& baseUrl, const char* path);

}  // namespace http
