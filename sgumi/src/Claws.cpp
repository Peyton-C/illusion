#include "Claws.hpp"

#include "Http.hpp"

#include <nlohmann/json.hpp>

#include <utility>

using json = nlohmann::json;

namespace claws {
namespace {

// Slower than lipgloss's. claws is on the NAS VM across the tailnet, nothing
// on it changes because of anything SGUMI does, and this poll exists only to
// say whether a lookup would work at all.
constexpr auto kPollInterval = std::chrono::seconds(5);

// claws returns JSON nulls for unset columns, location, notes and tags are
// all nullable -- and json::get<std::string> throws on those rather than
// yielding "". Every read goes through here.
std::string str(const json& data, const char* key, const char* fallback = "") {
    if (!data.contains(key) || data[key].is_null()) {
        return fallback;
    }

    if (data[key].is_string()) {
        return data[key].get<std::string>();
    }

    return data[key].dump();
}

Item itemFromJson(const json& data) {
    Item item;

    item.sku = str(data, "SKU");
    item.name = str(data, "NAME");
    item.location = str(data, "LOCATION");
    item.tags = str(data, "TAGS");
    item.notes = str(data, "NOTES");
    item.digikeyPartNumber = str(data, "DIGIKEY_PART_NUMBER");
    item.trackingMode = str(data, "TRACKING_MODE");

    if (data.contains("QUANTITY_ON_HAND") && data["QUANTITY_ON_HAND"].is_number()) {
        item.quantityOnHand = data["QUANTITY_ON_HAND"].get<long long>();
    }

    // A KANBAN item has no count, so LOW is the only signal it carries.
    if (data.contains("LOW") && data["LOW"].is_boolean()) {
        item.low = data["LOW"].get<bool>();
    }

    return item;
}

bool blank(const std::string& value) {
    return value.find_first_not_of(" \t\r\n") == std::string::npos;
}

}  // namespace

Client::~Client() {
    stop();
}

void Client::start() {
    if (running_.exchange(true)) {
        return;
    }

    worker_ = std::thread(&Client::run, this);
}

void Client::stop() {
    if (!running_.exchange(false)) {
        return;
    }

    wake_.notify_all();

    if (worker_.joinable()) {
        worker_.join();
    }
}

void Client::setEndpoint(std::string baseUrl, std::string token) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        baseUrl_ = std::move(baseUrl);
        token_ = std::move(token);

        // The old snapshot describes a different service; the old lookup came
        // from a different inventory. Both are dropped rather than shown
        // beside a new URL.
        snapshot_ = Snapshot {};
        lookup_ = Lookup {};
        catalog_ = Catalog {};
        pendingCatalog_ = false;
    }

    wake_.notify_all();
}

void Client::lookup(std::string sku) {
    if (blank(sku)) {
        clearLookup();
        return;
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);

        // Shown immediately so the button reads as having done something, even
        // though the request has not left this thread yet.
        lookup_ = Lookup {};
        lookup_.state = Lookup::State::Pending;
        lookup_.sku = sku;

        pendingLookup_ = std::move(sku);
    }

    wake_.notify_all();
}

void Client::clearLookup() {
    std::lock_guard<std::mutex> lock(mutex_);
    lookup_ = Lookup {};
    pendingLookup_.reset();
}

void Client::fetchCatalog() {
    {
        std::lock_guard<std::mutex> lock(mutex_);

        // Already have it, or already asking. The table only changes when
        // someone adds an item, which is not something worth re-checking every
        // time a range field is touched.
        if (catalog_.state == Catalog::State::Ready ||
            catalog_.state == Catalog::State::Pending) {
            return;
        }

        catalog_.state = Catalog::State::Pending;
        catalog_.error.clear();
        pendingCatalog_ = true;
    }

    wake_.notify_all();
}

Catalog Client::catalog() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return catalog_;
}

Lookup Client::lookupResult() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return lookup_;
}

Snapshot Client::snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return snapshot_;
}

void Client::run() {
    while (running_.load()) {
        // Lookups first: someone is watching a spinner, whereas the health
        // poll is background.
        std::optional<std::string> pending;
        bool wantCatalog = false;

        {
            std::lock_guard<std::mutex> lock(mutex_);
            pending.swap(pendingLookup_);
            wantCatalog = pendingCatalog_;
            pendingCatalog_ = false;
        }

        if (pending) {
            runLookup(*pending);
        }

        if (wantCatalog) {
            runCatalog();
        }

        pollHealth();

        std::unique_lock<std::mutex> lock(mutex_);

        // Predicated on both, so neither a stop nor a lookup waits out the
        // remaining interval.
        wake_.wait_for(lock, kPollInterval, [this] {
            return !running_.load() ||
                   pendingLookup_.has_value() ||
                   pendingCatalog_;
        });
    }
}

void Client::pollHealth() {
    std::string baseUrl;
    std::string token;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        baseUrl = baseUrl_;
        token = token_;
    }

    Snapshot next;

    if (baseUrl.empty()) {
        next.error = "No claws URL configured.";

        std::lock_guard<std::mutex> lock(mutex_);
        snapshot_ = std::move(next);
        return;
    }

    const http::Response health = http::get(http::join(baseUrl, "/health"), "");

    next.polled = std::chrono::steady_clock::now();

    if (!health.transportOk) {
        next.error = health.error;
    } else if (health.status != 200) {
        next.error = "GET /health returned HTTP " + std::to_string(health.status);
    } else {
        try {
            const json data = json::parse(health.body);

            next.version = str(data, "version", "unknown");

            if (data.contains("uptime_ms") && data["uptime_ms"].is_number()) {
                next.uptimeMs = data["uptime_ms"].get<long long>();
            }

            next.reachable = true;
        } catch (const json::exception& e) {
            next.error =
                std::string("GET /health did not return JSON: ") + e.what();
        }
    }

    std::lock_guard<std::mutex> lock(mutex_);

    // The token is not exercised by /health, which is unauthenticated. A
    // lookup is what discovers a bad token, so that verdict is preserved
    // across polls rather than reset to false here.
    next.unauthorized = snapshot_.unauthorized;
    snapshot_ = std::move(next);
}

void Client::runCatalog() {
    std::string baseUrl;
    std::string token;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        baseUrl = baseUrl_;
        token = token_;
    }

    Catalog result;
    result.state = Catalog::State::Failed;

    if (baseUrl.empty()) {
        result.error = "No claws URL configured. Set one in Settings.";
    } else {
        const http::Response response =
            http::get(http::join(baseUrl, "/items"), token);

        if (!response.transportOk) {
            result.error = response.error;
        } else if (response.status == 401 || response.status == 403) {
            result.error =
                "claws rejected the token. It must match claws.yaml's token.";
        } else if (response.status != 200) {
            result.error =
                "claws returned HTTP " + std::to_string(response.status);
        } else {
            try {
                const json data = json::parse(response.body);

                if (!data.is_array()) {
                    throw json::type_error::create(
                        302, "expected an array of items", &data);
                }

                for (const json& row : data) {
                    const std::string sku = str(row, "SKU");

                    if (!sku.empty()) {
                        result.names[sku] = str(row, "NAME");
                    }
                }

                result.state = Catalog::State::Ready;
            } catch (const json::exception& e) {
                result.error =
                    std::string("claws did not return JSON: ") + e.what();
            }
        }
    }

    std::lock_guard<std::mutex> lock(mutex_);

    if (result.state == Catalog::State::Ready) {
        snapshot_.unauthorized = false;
    }

    catalog_ = std::move(result);
}

void Client::runLookup(const std::string& sku) {
    std::string baseUrl;
    std::string token;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        baseUrl = baseUrl_;
        token = token_;
    }

    Lookup result;
    result.sku = sku;

    if (baseUrl.empty()) {
        result.state = Lookup::State::Failed;
        result.error = "No claws URL configured. Set one in Settings.";

        std::lock_guard<std::mutex> lock(mutex_);
        lookup_ = std::move(result);
        return;
    }

    const http::Response response =
        http::get(http::join(baseUrl, "/items/") + sku, token);

    if (!response.transportOk) {
        result.state = Lookup::State::Failed;
        result.error = response.error;
    } else if (response.status == 404) {
        // A real answer, not a failure: claws looked and there is no such SKU.
        result.state = Lookup::State::NotFound;
    } else if (response.status == 401 || response.status == 403) {
        result.state = Lookup::State::Failed;
        result.error =
            "claws rejected the token. It must match claws.yaml's token.";

        std::lock_guard<std::mutex> lock(mutex_);
        snapshot_.unauthorized = true;
        lookup_ = std::move(result);
        return;
    } else if (response.status != 200) {
        result.state = Lookup::State::Failed;
        result.error = "claws returned HTTP " + std::to_string(response.status);
    } else {
        try {
            result.item = itemFromJson(json::parse(response.body));
            result.state = Lookup::State::Found;
        } catch (const json::exception& e) {
            result.state = Lookup::State::Failed;
            result.error = std::string("claws did not return JSON: ") + e.what();
        }
    }

    std::lock_guard<std::mutex> lock(mutex_);

    // A successful lookup proves the token is good, which /health cannot.
    if (result.state == Lookup::State::Found ||
        result.state == Lookup::State::NotFound) {
        snapshot_.unauthorized = false;
    }

    lookup_ = std::move(result);
}

}  // namespace claws
