#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <thread>

// Client for the claws inventory service.
//
// SGUMI only reads from claws, and only to answer one question: what is this
// SKU? lipgloss deliberately knows nothing about inventory, see the module
// docstring in packages/lipgloss/src/lipgloss/service.py, which says callers
// wanting an item name on a label must resolve it themselves and pass literal
// text. This class is that resolution step.
//
// Nothing here writes. 

namespace claws {

// One row of GET /items/{sku}. The wire keys are upper-case because the same
// dicts feed illusion's terminal table renderer.
//
// Only the fields worth putting on a label or showing beside one; the real
// item also carries vendors, links, thresholds and a low-thread id.
struct Item {
    std::string sku;
    std::string name;
    std::string location;
    std::string tags;
    std::string notes;
    std::string digikeyPartNumber;
    std::string trackingMode;
    long long quantityOnHand = 0;
    bool low = false;
};

// The state of one SKU lookup. Lives as a single value rather than a queue:
// the UI has one SKU box, so a second lookup replaces the first rather than
// joining it.
struct Lookup {
    enum class State {
        Idle,      // nothing asked for yet
        Pending,   // in flight
        Found,
        NotFound,  // claws answered 404, which is a real answer
        Failed,    // could not ask, or claws refused
    };

    State state = State::Idle;

    // What was asked for, so a reply arriving after the box was edited can be
    // recognised as stale.
    std::string sku;

    Item item;
    std::string error;
};

struct Snapshot {
    bool reachable = false;
    bool unauthorized = false;
    std::string error;
    std::string version;
    long long uptimeMs = 0;
    std::chrono::steady_clock::time_point polled {};
};

// Every SKU claws knows, as SKU -> item name.
//
// Fetched in one GET /items rather than a request per SKU: a range can span
// hundreds of labels, and the whole table is a few hundred rows. One request
// that is occasionally larger than needed beats four hundred that are not.
struct Catalog {
    enum class State {
        Idle,
        Pending,
        Ready,
        Failed,
    };

    State state = State::Idle;
    std::map<std::string, std::string> names;
    std::string error;
};

class Client {
public:
    Client() = default;
    ~Client();

    Client(const Client&) = delete;
    Client& operator=(const Client&) = delete;

    void start();
    void stop();

    void setEndpoint(std::string baseUrl, std::string token);

    // Asks for a SKU. Returns immediately; watch lookupResult() for the
    // answer. An empty or whitespace-only sku clears the result instead.
    void lookup(std::string sku);

    void clearLookup();

    // Fetches the whole catalogue, for range printing. Returns immediately;
    // watch catalog() for the answer. Calling it while one is in flight, or
    // when one is already loaded, does nothing -- the table does not change
    // often enough to be worth refetching on every keystroke.
    void fetchCatalog();

    // Both thread-safe, both return copies so a frame can hold one without
    // blocking the worker.
    Lookup lookupResult() const;
    Snapshot snapshot() const;
    Catalog catalog() const;

private:
    void run();
    void pollHealth();
    void runLookup(const std::string& sku);
    void runCatalog();

    mutable std::mutex mutex_;
    std::condition_variable wake_;

    std::string baseUrl_;
    std::string token_;

    Snapshot snapshot_;
    Lookup lookup_;
    Catalog catalog_;
    bool pendingCatalog_ = false;

    // Set by lookup(), taken by the worker. One slot, not a queue.
    std::optional<std::string> pendingLookup_;

    std::atomic<bool> running_ { false };
    std::thread worker_;
};

}  // namespace claws
