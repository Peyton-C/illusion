// SGUMI -- The Super Graphic Ultra Modern Interface.
//
// illusion's desktop frontend for lipgloss.
//
// The window, the frame loop, the fatal-error reporting and the config file
// handling below are ported from Software_2027's apps/frontend/src/main.cpp at
// commit ff6a7f6. Everything related to ROVs was dropped. 
//
// See CMakeLists.txt for how to diff the bones against that repo later.

#include "Claws.hpp"
#include "Http.hpp"
#include "Image.hpp"
#include "Lipgloss.hpp"
#include "Paths.hpp"
#include "Theme.hpp"
#include "build_info.h"

#include <SDL3/SDL.h>
#include <SDL3/SDL_main.h>
#include "imgui.h"
#include "backends/imgui_impl_sdl3.h"
#include "backends/imgui_impl_sdlgpu3.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <mutex>
#include <string>

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace {

// Matches StartupWMClass in the .desktop file CMakeLists.txt generates. If one
// changes the other has to, or xfce shows the running window as a second,
// unnamed taskbar entry beside the launcher.
constexpr const char* kAppId = "com.easternedgerobotics.sgumi";

// The client's own limits, in the unit the setting is typed in. Derived rather
// than written out twice, so the box cannot offer a number the client would
// quietly clamp away.
constexpr int kMinPollSeconds =
    static_cast<int>(lipgloss::kMinPollInterval.count() / 1000);
constexpr int kMaxPollSeconds =
    static_cast<int>(lipgloss::kMaxPollInterval.count() / 1000);

// Fixed-size buffers because ImGui::InputText writes into a char array. 512 is
// what the 2027 frontend uses for the same job and is far past any real URL.
struct Config {
    char lipglossUrl[512] = "http://127.0.0.1:8081";
    char lipglossToken[512] = "";

    // claws lives on the NAS VM in production, so this default is only right
    // for a development checkout running all four services locally. See
    // kiosk.example.yaml for what the closet laptop actually points at.
    char clawsUrl[512] = "http://127.0.0.1:8080";
    char clawsToken[512] = "";

    // Seconds between queue polls.
    int pollSeconds = 5;
};

Config g_config;

// The print form. Separate from Config because none of it is persisted -- a
// label typed yesterday is not something to restore on launch.
struct PrintForm {
    int styleIndex = 0;
    char sku[64] = "";
    char line1[128] = "";
    char line2[128] = "";
    int copies = 1;

    // The bot's get_text_from_sku, as a toggle: while on, line 1 is the item
    // name claws returns rather than anything typed. On by default because
    // that is the whole point of SGUMI knowing about claws -- scan, name
    // appears, print -- and it is trivially switched off for a one-off label.
    bool useSkuName = true;

    // Which lookup has already been copied into line 1, so the fill happens
    // once per lookup rather than every frame -- otherwise editing line 1
    // would be undone on the next frame.
    std::string filledFromSku;

    // requestSignature() of whatever the showing preview was rendered from, so
    // an edit since can be pointed out rather than leaving a stale picture
    // looking current.
    std::string previewOf;

    // Whether the picture on screen is the example rather than the form's own
    // label. Suppresses the staleness note, which would otherwise fire
    // immediately -- the example never matches the form.
    bool previewIsExample = false;

    // Latched once per session. Without it, anything that clears the preview
    // would bring the example back after the user had moved on from it.
    bool exampleRequested = false;

    // Something changed the label but the form was not printable yet, so the
    // refresh is owed rather than done. Survives frames, unlike the local
    // `changed` flag, which is why filling in the last field of a range
    // finally produces a preview.
    bool previewStale = false;

    // Range mode: one label per SKU across a span, instead of one label.
    // Swaps out most of the form, so it is a mode rather than a second panel.
    bool rangeMode = false;

    // Held as text, not numbers. The endpoint wants integers, but a SKU is
    // what is printed on the bin and what a scanner types, so these accept
    // either "EER-000421" or "421" and skuNumber() pulls the number out.
    char rangeFrom[64] = "";
    char rangeTo[64] = "";
};

// The image style's own form. Separate from PrintForm because none of it
// applies to a rendered label, and because the decoded bitmaps are large enough
// that carrying them around in the struct every style touches would be untidy.
struct ImageForm {
    // The file as it was picked, decoded once. Kept so turning or refitting
    // does not re-read the disk, and so repeated fits do not compound their
    // own resampling losses.
    image::Bitmap source;

    std::string path;
    std::string name;   // basename, and the description when none is typed
    std::string error;  // why the last pick did not load

    char description[128] = "";
    int copies = 1;

    // Quarter turns clockwise. A phone photo is landscape and the label is
    // narrow, so turning it is the common case rather than an exotic one.
    int quarterTurns = 0;

    // Whether the image is distorted to the label's shape or fitted inside it
    // with white to spare. Either way the label itself comes out whole: the
    // stock is precut at 12x40mm, so an image that covers a third of it is not
    // a smaller label, just a mostly empty one.
    //
    // On by default because using the label is the usual want, and the label is
    // long and thin while almost nothing anyone picks is.
    bool stretch = true;

    // What the knobs above produce, and what actually gets sent. Rebuilt when
    // something changes rather than every frame -- a resample of a twelve
    // megapixel photo is not a per-frame cost.
    image::Bitmap prepared;
    std::string png;
    bool dirty = false;

    // Bumped when png is replaced, so the preview panel re-uploads only then.
    // Its own counter rather than the lipgloss preview's: the two are different
    // pictures with different lifetimes.
    unsigned long long serial = 0;

    // The serial already sent to lipgloss for dithering. Compared rather than
    // flagged, so a preview is asked for exactly once per prepared image no
    // matter how many frames pass before the answer lands.
    unsigned long long previewedSerial = 0;
};

ImageForm g_image;

// Filled by SDL's file dialog callback and drained by the frame loop. The SDL
// docs are explicit that the callback may run on another thread, so this is the
// handover rather than writing into g_image directly.
struct PickedFile {
    std::mutex mutex;
    std::string path;
    std::string error;

    // A dialog is open. Keeps the button from stacking a second one.
    bool waiting = false;

    // Something to consume. A cancel clears waiting without setting this.
    bool ready = false;
};

PickedFile g_picked;

PrintForm g_print;

// ---------------------------------------------------------------------------
// Config file
// ---------------------------------------------------------------------------

// ImGui hands back a char array; json wants a std::string. Guarding on the key
// existing means a config written by an older build, missing a field added
// since, keeps the built-in default rather than becoming an empty string.
void copyJsonString(const json& data, const char* key, char* out, size_t size) {
    if (!data.contains(key) || !data[key].is_string()) {
        return;
    }

    const std::string value = data[key].get<std::string>();
    std::snprintf(out, size, "%s", value.c_str());
}

bool saveConfigToFile(const fs::path& path, const Config& config) {
    std::error_code ec;

    if (path.has_parent_path()) {
        fs::create_directories(path.parent_path(), ec);

        if (ec) {
            std::cerr << "Failed to create config directory: "
                      << path.parent_path() << std::endl;
            return false;
        }
    }

    std::ofstream output(path);

    if (!output) {
        std::cerr << "Failed to open config file for writing: " << path
                  << std::endl;
        return false;
    }

    const json data = {
        {"lipgloss_url", config.lipglossUrl},
        {"lipgloss_token", config.lipglossToken},
        {"claws_url", config.clawsUrl},
        {"claws_token", config.clawsToken},
        {"poll_seconds", config.pollSeconds},
    };

    output << data.dump(4) << std::endl;
    return true;
}

bool loadConfigFromFile(const fs::path& path, Config& config) {
    if (!fs::exists(path)) {
        std::cerr << "Config file does not exist. Creating default config: "
                  << path << std::endl;

        saveConfigToFile(path, config);
        return false;
    }

    std::ifstream input(path);

    if (!input) {
        std::cerr << "Failed to open config file: " << path << std::endl;
        return false;
    }

    try {
        json data;
        input >> data;

        copyJsonString(data, "lipgloss_url", config.lipglossUrl,
                       sizeof(config.lipglossUrl));
        copyJsonString(data, "lipgloss_token", config.lipglossToken,
                       sizeof(config.lipglossToken));
        copyJsonString(data, "claws_url", config.clawsUrl,
                       sizeof(config.clawsUrl));
        copyJsonString(data, "claws_token", config.clawsToken,
                       sizeof(config.clawsToken));

        // Clamped to the same range the client enforces.
        if (data.contains("poll_seconds") &&
            data["poll_seconds"].is_number_integer()) {
            config.pollSeconds = std::clamp(
                data["poll_seconds"].get<int>(),
                kMinPollSeconds,
                kMaxPollSeconds);
        }
    } catch (const json::exception& e) {
        // Not fatal. A corrupt config costs the saved URL, not the session --
        // the window still opens and the settings can be retyped.
        std::cerr << "Failed to parse config file: " << e.what() << std::endl;
        return false;
    }

    return true;
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

// Every fatal path below used to just print and return -1, which is fine at a
// terminal and useless anywhere there is no console to read -- a double-click
// on the kiosk's desktop, or an .app launched from Finder. So the same message
// also goes to a message box, which SDL renders natively.
//
// Before SDL_Init there is no video subsystem to put a box on, so the call
// fails and this degrades to the stderr line it always was.
void reportFatal(const char* stage, const char* detail) {
    std::cerr << stage << ": " << detail << std::endl;
    SDL_ShowSimpleMessageBox(SDL_MESSAGEBOX_ERROR, stage, detail, nullptr);
}

// ---------------------------------------------------------------------------
// UI
// ---------------------------------------------------------------------------

std::string formatUptime(long long ms) {
    if (ms <= 0) {
        return "unknown";
    }

    const long long seconds = ms / 1000;
    const long long days = seconds / 86400;
    const long long hours = (seconds % 86400) / 3600;
    const long long minutes = (seconds % 3600) / 60;

    char buffer[64];

    if (days > 0) {
        std::snprintf(buffer, sizeof(buffer), "%lldd %lldh %lldm",
                      days, hours, minutes);
    } else if (hours > 0) {
        std::snprintf(buffer, sizeof(buffer), "%lldh %lldm", hours, minutes);
    } else {
        std::snprintf(buffer, sizeof(buffer), "%lldm %llds",
                      minutes, seconds % 60);
    }

    return buffer;
}

// ---------------------------------------------------------------------------
// Label styles
// ---------------------------------------------------------------------------

// The choices as the user sees them, matching the bot's /print command so the
// two front ends offer the same menu.
//
// Two of these are not real lipgloss styles. "label" and "label_qr" are
// resolved to their 1-line or 2-line variant by resolveStyle() below, based on
// whether line 2 was filled in -- lipgloss itself only knows the resolved
// names. classic_barcode is deliberately absent: it exists for /render, not
// for the label printer.
struct Style {
    const char* value;
    const char* label;
    bool needsSku;
    bool needsLine1;

    // Whether line 2 does anything for this style. False greys the field out
    // rather than silently ignoring what gets typed there.
    bool usesLine2;

    // The odd one out: not a lipgloss style at all. It has no name on the wire,
    // goes to /print/image instead of /print, and swaps the form for a picker
    // the way Range print does. It lives in this list because "what am I
    // printing" is one question, and a second picker beside the style one would
    // be two controls for it.
    bool isImage;
};

constexpr Style kStyles[] = {
    { "slim_barcode",    "Barcode",                true,  false, false, false },
    { "label_barcode",   "Label w/ Barcode",       true,  true,  false, false },
    { "label_qr",        "Label w/ QR Code",       true,  true,  true,  false },
    { "label",           "Label",                  false, true,  true,  false },
    { "cable_label",     "Cable Label",            false, true,  true,  false },
    { "cable_label_sku", "Cable Label w/ SKU",     true,  true,  true,  false },
    { "cable_label_qr",  "Cable Label w/ QR Code", true,  true,  false, false },
    { "",                "Image",                  false, false, false, true  },
};

constexpr int kStyleCount = static_cast<int>(sizeof(kStyles) / sizeof(kStyles[0]));

bool blank(const char* text) {
    for (const char* c = text; *c; ++c) {
        if (*c != ' ' && *c != '\t' && *c != '\r' && *c != '\n') {
            return false;
        }
    }

    return true;
}

// Mirrors illusion_helpers.clean_sku: anything six characters or shorter is
// padded into the full EER-nnnnnn form, so typing 421 finds EER-000421.
std::string cleanSku(const std::string& sku) {
    if (sku.empty() || sku.size() > 6) {
        return sku;
    }

    return "EER-" + std::string(6 - sku.size(), '0') + sku;
}

// Turns a picked style plus the filled-in fields into what lipgloss actually
// understands. Mirrors print_niimbot in illusion-bot's __main__.py; if the
// pairings change there they have to change here.
//
// The cable styles are the odd ones out: they always render two rows, so an
// empty second line repeats the first rather than leaving a blank half.
std::string resolveStyle(
    const Style& style,
    const std::string& line1,
    std::string& line2)
{
    const std::string name = style.value;

    if (line2.empty() && (name == "cable_label" || name == "cable_label_sku")) {
        line2 = line1;
    }

    if (name == "label") {
        return line2.empty() ? "label_1_line" : "label_2_line";
    }

    if (name == "label_qr") {
        return line2.empty() ? "label_1_line_qr" : "label_2_line_qr";
    }

    return name;
}

// The SKU lipgloss will render for a given number in a range. Mirrors the
// f"EER-{number:06d}" in print_barcodes, so a name looked up under this key
// lands on the label that number produces.
std::string skuForNumber(int number) {
    char buffer[32];
    std::snprintf(buffer, sizeof(buffer), "EER-%06d", number);
    return buffer;
}

// What one label of a range should say. claws's name for the SKU, or the SKU
// itself when it has none -- a bin whose item was never added still gets a
// label that identifies it rather than a blank line or a failed job.
std::string rangeName(claws::Client& client, const std::string& sku) {
    const claws::Catalog catalog = client.catalog();
    const auto it = catalog.names.find(sku);

    if (it != catalog.names.end() && !it->second.empty()) {
        return it->second;
    }

    return sku;
}

// The style name a range should send.
//
// Same resolution as a single print, but with no text to go on: a range has no
// line 2, so the styles that choose between a one- and two-line variant always
// take the one-line one. lipgloss fills whatever text cells remain with the
// SKU itself.
std::string rangeStyle(const Style& style) {
    std::string line2;
    return resolveStyle(style, "", line2);
}

// ---------------------------------------------------------------------------
// The image style
// ---------------------------------------------------------------------------

// The label itself: LABEL_WIDTH and LABEL_HEIGHT in lipgloss's service.py, the
// geometry every rendered style is drawn at.
//
// The stock is precut at 12x40mm and the head lays down 8 pixels to the
// millimetre, which is where both numbers come from -- 12mm across is 96 and
// 40mm along is 320. They are not a choice anything here gets to make.
//
// Named for the printed orientation. lipgloss draws a label 320x96 and turns it
// a quarter before printing, so the file the head receives is 96 across and 320
// long. It turns nothing for an image, so this is the size SGUMI has to produce
// itself, already the right way round.
constexpr int kLabelPrintWidth = 96;    // 12mm, LABEL_HEIGHT over there
constexpr int kLabelPrintLength = 320;  // 40mm, LABEL_WIDTH over there

// PRINTER_MAX_WIDTH in packages/lipgloss/src/lipgloss/printer.py, keyed by the
// model GET /health reports. lipgloss refuses a wider image when the job is
// submitted, so having the number here is what lets the image be fitted before
// the upload instead of after the refusal.
//
// Matched exactly rather than case-insensitively, because lipgloss does a plain
// dict lookup: a model spelled differently gets no limit on either side, which
// is at least the same answer in both places.
//
// 0 means no limit, which is what an unlisted model gets over there too.
int maxLabelWidth(const std::string& model) {
    if (model == "b1" || model == "b18" || model == "b21") {
        return 384;
    }

    if (model == "d11" || model == "d110") {
        return 96;
    }

    return 0;
}

// The filename, for the default description and the "what is loaded" line.
std::string baseName(const std::string& path) {
    const size_t slash = path.find_last_of("/\\");

    return slash == std::string::npos ? path : path.substr(slash + 1);
}

// Turn, then fill the label, then encode. In that order because the label is
// long in one direction and the turn is what decides which of the image's sides
// runs along it -- fitting first would measure against the wrong edge.
//
// The result is always the whole label, 96 by 320. The stock is precut, so a
// smaller image does not print a smaller label, it prints the same label with
// most of it left blank.
void prepareImage(int maxWidth) {
    g_image.dirty = false;
    g_image.prepared = image::Bitmap {};
    g_image.png.clear();
    g_image.serial++;

    if (!g_image.source.valid()) {
        return;
    }

    image::Bitmap working = image::rotated(g_image.source, g_image.quarterTurns);

    // The label is 96 across on every model that reports a width, since 96 is
    // the stock and not the printer. Clamped anyway, so a printer narrower than
    // the label would be fitted to rather than refused.
    const int targetWidth = maxWidth > 0
        ? std::min(kLabelPrintWidth, maxWidth)
        : kLabelPrintWidth;
    const int targetLength = kLabelPrintLength;

    if (g_image.stretch) {
        // Straight to the label's shape, aspect ratio and all. A square logo
        // comes out three times taller than it is wide, which is the trade the
        // checkbox is offering.
        working = image::scaled(working, targetWidth, targetLength);
    } else {
        // As large as fits with the proportions intact, then centred. Whichever
        // side runs out first sets the scale, and the other gets white.
        const double byWidth =
            static_cast<double>(targetWidth) / static_cast<double>(working.width);
        const double byLength =
            static_cast<double>(targetLength) / static_cast<double>(working.height);
        const double ratio = std::min(byWidth, byLength);

        // Floored at one so a very long, very thin image cannot round away to
        // nothing on its short side.
        working = image::scaled(
            working,
            std::max(1, static_cast<int>(working.width * ratio + 0.5)),
            std::max(1, static_cast<int>(working.height * ratio + 0.5)));

        working = image::paddedTo(working, targetWidth, targetLength);
    }

    g_image.prepared = std::move(working);

    if (!image::encodePng(g_image.prepared, g_image.png)) {
        g_image.error = "That image could not be encoded as a PNG.";
        g_image.prepared = image::Bitmap {};
    }
}

// Reads a picked file and makes it the loaded image. Anything already loaded is
// kept when this fails, so a mistyped path does not clear a good one.
void loadImage(const std::string& path, int maxWidth) {
    image::Bitmap decoded;
    std::string error;

    if (!image::decodeFile(path.c_str(), decoded, error)) {
        g_image.error = error;
        return;
    }

    g_image.error.clear();
    g_image.source = std::move(decoded);
    g_image.path = path;
    g_image.name = baseName(path);

    // The filename is the description unless one has been typed. It is a far
    // better queue row than a blank, and it is what someone would have typed
    // anyway.
    if (blank(g_image.description)) {
        std::snprintf(g_image.description, sizeof(g_image.description), "%s",
                      g_image.name.c_str());
    }

    // A new file is measured against the same knobs the last one used, which is
    // what someone printing a batch of images expects.
    prepareImage(maxWidth);
}

void SDLCALL onFilePicked(void* userdata, const char* const* filelist, int) {
    auto* picked = static_cast<PickedFile*>(userdata);

    std::lock_guard<std::mutex> lock(picked->mutex);
    picked->waiting = false;

    if (!filelist) {
        picked->error = SDL_GetError();
        picked->ready = true;
        return;
    }

    // An empty list is a cancel, which is not an outcome worth reporting.
    if (!filelist[0]) {
        return;
    }

    picked->path = filelist[0];
    picked->error.clear();
    picked->ready = true;
}

// The image style's equivalent of printBlocker.
//
// There is deliberately no "too wide" case. prepareImage reduces anything
// oversize whatever the width setting says, so the only way to reach lipgloss's
// own refusal would be a bug in here -- and a check for it would be a branch
// that never runs and never gets tested.
const char* imageBlocker() {
    if (!g_image.source.valid()) {
        return "Choose an image.";
    }

    if (!g_image.prepared.valid() || g_image.png.empty()) {
        return "That image could not be prepared.";
    }

    return nullptr;
}

// Why the Print button is disabled, or nullptr when it is not. lipgloss checks
// this too and answers 200 with a refusal message, but saying it here means
// the round trip is not needed to find out.
const char* printBlocker(const Style& style) {
    if (style.needsSku && blank(g_print.sku)) {
        return "This style needs a SKU.";
    }

    if (style.needsLine1 && blank(g_print.line1)) {
        return "This style needs line 1.";
    }

    return nullptr;
}

constexpr ImVec4 kGood { 0.35f, 0.80f, 0.45f, 1.0f };
constexpr ImVec4 kWarn { 0.90f, 0.65f, 0.30f, 1.0f };
constexpr ImVec4 kBad  { 0.88f, 0.36f, 0.36f, 1.0f };

// Which pane owns an outcome. The client keeps one result slot for every
// action, so each pane has to ignore the results that are not its own --
// without this a cancel would be announced under the Print button, where the
// only wording available is about a job that was never submitted.
bool isQueueAction(lipgloss::ActionResult::Kind kind) {
    return kind == lipgloss::ActionResult::Kind::Resume ||
           kind == lipgloss::ActionResult::Kind::Clear ||
           kind == lipgloss::ActionResult::Kind::Cancel;
}

// The state of lipgloss in 3 words
struct Status {
    ImVec4 colour;
    const char* label;
};

Status statusOf(const lipgloss::Snapshot& snapshot) {
    if (!snapshot.reachable) {
        return { kBad, "Unreachable" };
    }

    if (snapshot.unauthorized) {
        return { kWarn, "Token rejected" };
    }

    if (snapshot.paused) {
        return { kWarn, "Queue paused" };
    }

    return { kGood, "Connected" };
}

// The indicator and the refresh button, drawn inside the menu bar.
// SameLine isnt required because BeginMenuBar already handles that
void drawStatusBar(const lipgloss::Snapshot& snapshot, lipgloss::Client& client) {
    const Status status = statusOf(snapshot);
    const ImGuiStyle& style = ImGui::GetStyle();

    constexpr const char* kDot = "\xe2\x97\x8f";  // U+25CF BLACK CIRCLE

    // Right alligned
    // Calculated based off the width of whats about to be drawn, because imgui
    // doesn't have a way to natively handle this 
    // 
    // SmallButton's width is its label plus FramePadding.x on each side, it
    // zeroes only the vertical padding. The two ItemSpacing gaps are the ones
    // the horizontal layout will insert between the three items.
    const float width =
        ImGui::CalcTextSize(kDot).x +
        ImGui::CalcTextSize(status.label).x +
        ImGui::CalcTextSize("Refresh").x + style.FramePadding.x * 2.0f +
        style.ItemSpacing.x * 2.0f;

    const float avail = ImGui::GetContentRegionAvail().x;

    // Offset from where the cursor already is rather than an absolute X
    if (avail > width) {
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + avail - width);
    }

    ImGui::TextColored(status.colour, "%s", kDot);
    bool hovered = ImGui::IsItemHovered();

    ImGui::TextUnformatted(status.label);
    hovered = hovered || ImGui::IsItemHovered();

    // Checked on both halves so hovering the dot works as well as the word.
    if (hovered && !snapshot.error.empty()) {
        ImGui::SetTooltip("%s", snapshot.error.c_str());
    }

    if (ImGui::Button("Refresh")) {
        client.refresh();
    }
}

// A two-column key/value block: dimmed label on the left, value on the right,
// columns sized to their content. Every block on the About page is built from
// these two calls so they read as one kind of thing rather than as several.
bool beginInfoTable(const char* id) {
    return ImGui::BeginTable(id, 2, ImGuiTableFlags_SizingFixedFit);
}

void infoRow(const char* key, const char* value) {
    ImGui::TableNextRow();
    ImGui::TableNextColumn();
    ImGui::TextDisabled("%s", key);
    ImGui::TableNextColumn();
    ImGui::TextUnformatted(value);
}

// What this binary is. Fixed for the life of the process.
void drawBuildInfo(SDL_GPUDevice* gpuDevice) {
    if (!beginInfoTable("build")) {
        return;
    }

    infoRow("Version", SGUMI_VERSION);
    infoRow("Built", EER_BUILD_DATE);

    // Theoretically it should be impossible for this to be needed, because
    // a gpu is needed to even render this, but its good practice to have
    // a fallback, even if its realistically useless.
    const char* driver = SDL_GetGPUDeviceDriver(gpuDevice);
    infoRow("Renderer", driver ? driver : "unknown");

    ImGui::EndTable();
}

// lipgloss's own identity and health, from GET /health.
//
// The event stream is reported here rather than in the status bar for the same
// reason claws is: it not being up doesn't stop anything printing. It costs
// latency -- the queue is then only as current as the poll interval -- and that
// is a thing to be able to look up, not a thing to interrupt anyone about.
void drawServiceInfo(const lipgloss::Snapshot& snapshot, bool eventsConnected,
                     int pollSeconds) {
    if (!snapshot.reachable) {
        ImGui::TextDisabled("Not connected.");
        return;
    }

    if (!beginInfoTable("health")) {
        return;
    }

    infoRow("Version", snapshot.version.c_str());
    infoRow("Model", snapshot.model.c_str());
    infoRow("Printer port", snapshot.printerPort.c_str());
    infoRow("Uptime", formatUptime(snapshot.uptimeMs).c_str());

    const std::string events = eventsConnected
        ? "Live"
        : "Not connected, polling every " + std::to_string(pollSeconds) + "s";

    infoRow("Events", events.c_str());

    ImGui::EndTable();
}

// claws, which only matters here for whether a SKU lookup would work. It gets
// no place in the status bar for that reason -- printing works fine without
// it, you just have to type the label text yourself.
void drawClawsInfo(const claws::Snapshot& snapshot) {
    if (!snapshot.reachable) {
        ImGui::TextDisabled("Not connected.");

        if (!snapshot.error.empty()) {
            ImGui::PushTextWrapPos(0.0f);
            ImGui::TextDisabled("%s", snapshot.error.c_str());
            ImGui::PopTextWrapPos();
        }

        return;
    }

    if (!beginInfoTable("clawsHealth")) {
        return;
    }

    infoRow("Version", snapshot.version.c_str());
    infoRow("Uptime", formatUptime(snapshot.uptimeMs).c_str());
    infoRow("Token", snapshot.unauthorized ? "Rejected" : "OK");

    ImGui::EndTable();
}

// ---------------------------------------------------------------------------
// Form layout
//
// ImGui puts a widget's label to its *right*, which reads badly in a form.
// These put the label in a fixed left column instead, and are written so a row
// can hold two label+field pairs -- the offset is measured from where each
// label starts, not from the edge of the window, so the second pair on a row
// lines up the same way the first does.
// ---------------------------------------------------------------------------

constexpr float kLabelWidth = 76.0f;
constexpr float kFieldMaxWidth = 220.0f;

// One pair filling the row.
float fieldWidth() {
    const float avail = ImGui::GetContentRegionAvail().x - kLabelWidth;
    return std::min(std::max(avail, 80.0f), kFieldMaxWidth);
}

// One of two pairs sharing the row.
//
// Must be called before either label is drawn, and the answer used for both
// fields. fieldLabel moves the cursor, so asking again halfway along the row
// measures what the first pair left rather than half the row -- which took the
// second field down to the 70 pixel floor below, and an InputInt at 70 pixels
// is two step buttons with no room left for the number they step. That is the
// whole reason the quantity box looked like a pair of buttons.
//
// Floors rather than going negative when the window is dragged narrow.
float halfFieldWidth() {
    const float avail = ImGui::GetContentRegionAvail().x;
    const float perPair = (avail - ImGui::GetStyle().ItemSpacing.x) * 0.5f;
    return std::min(std::max(perPair - kLabelWidth, 70.0f), kFieldMaxWidth);
}

// AlignTextToFramePadding so the label sits on the widget's baseline rather
// than riding up against the top of its frame. The cursor is then put a fixed
// distance past where *this* label began, which is what lets a second pair on
// the same row align like the first.
void fieldLabel(const char* text) {
    const float x = ImGui::GetCursorPosX();

    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(text);
    ImGui::SameLine();
    ImGui::SetCursorPosX(x + kLabelWidth);
}

// A range endpoint takes plain numbers, but these boxes take SKUs, because a
// SKU is what is printed on the bin and what a scanner types. Accepts either
// form: "EER-000421" and "421" both give 421.
bool skuNumber(const char* text, int& out) {
    std::string value = text;

    const size_t dash = value.find_last_of('-');

    if (dash != std::string::npos) {
        value = value.substr(dash + 1);
    }

    if (value.empty() ||
        value.find_first_not_of("0123456789") != std::string::npos) {
        return false;
    }

    // strtol rather than stoi: no exception to catch on a number too long to
    // fit, which someone leaning on a keypad will produce eventually.
    errno = 0;
    const long parsed = std::strtol(value.c_str(), nullptr, 10);

    if (errno != 0 || parsed < 0 || parsed > 999999) {
        return false;
    }

    out = static_cast<int>(parsed);
    return true;
}

// What claws said about the SKU in the box, and the fill of line 1.
//
// The fill is what the bot calls get_text_from_sku: the item's name becomes
// the label's first line. Applied once per lookup rather than every frame, so
// that the toggle being on does not re-clobber the field continuously.
void drawLookupResult(const claws::Lookup& lookup) {
    switch (lookup.state) {
    case claws::Lookup::State::Idle:
        return;

    case claws::Lookup::State::Pending:
        ImGui::TextDisabled("Looking up %s...", lookup.sku.c_str());
        return;

    case claws::Lookup::State::NotFound:
        ImGui::TextColored(kWarn, "No item with SKU %s.", lookup.sku.c_str());
        return;

    case claws::Lookup::State::Failed:
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextColored(kBad, "%s", lookup.error.c_str());
        ImGui::PopTextWrapPos();
        return;

    case claws::Lookup::State::Found:
        break;
    }

    if (g_print.filledFromSku != lookup.sku) {
        std::snprintf(g_print.line1, sizeof(g_print.line1), "%s",
                      lookup.item.name.c_str());
        g_print.filledFromSku = lookup.sku;
    }

    ImGui::PushTextWrapPos(0.0f);
    ImGui::TextColored(kGood, "%s", lookup.item.name.c_str());
    ImGui::PopTextWrapPos();

    // Stock is worth a glance while printing a label for a bin, but a KANBAN
    // item genuinely has no count -- showing 0 for one would read as empty.
    std::string detail;

    if (lookup.item.trackingMode != "KANBAN") {
        detail = std::to_string(lookup.item.quantityOnHand) + " on hand";
    }

    if (!lookup.item.location.empty()) {
        if (!detail.empty()) {
            detail += ", ";
        }

        detail += "at " + lookup.item.location;
    }

    if (!detail.empty()) {
        ImGui::TextDisabled("%s", detail.c_str());
    }

    if (lookup.item.low) {
        ImGui::SameLine();
        ImGui::TextColored(kWarn, "LOW");
    }
}

// The form as lipgloss wants it. Used for both Print and Preview, so the
// preview is guaranteed to render the label the print would.
lipgloss::PrintRequest buildRequest(const Style& style) {
    lipgloss::PrintRequest request;

    std::string line2 = g_print.line2;
    request.style = resolveStyle(style, g_print.line1, line2);

    // Only when the style actually puts it on the label. The box can hold a
    // SKU for a style that does not use one -- that is how claws is asked for
    // the item name -- and sending it anyway would change what gets rendered.
    request.sku = style.needsSku ? cleanSku(g_print.sku) : "";

    request.line1 = style.needsLine1 ? g_print.line1 : "";
    request.line2 = style.usesLine2 ? line2 : "";
    request.copies = g_print.copies;

    return request;
}

// Everything the preview depends on, flattened. Compared against what the
// showing preview was made from, to notice when it has gone stale -- copies is
// left out on purpose, since printing three of a label does not change how it
// looks.
std::string requestSignature(const Style& style) {
    if (g_print.rangeMode) {
        // A range's preview is only ever its first label, so the style and
        // that one SKU are the only things that change what is shown.
        return "range\x1f" + rangeStyle(style) + '\x1f' + g_print.rangeFrom +
               '\x1f' + g_print.line2;
    }

    const lipgloss::PrintRequest request = buildRequest(style);

    return request.style + '\x1f' + request.sku + '\x1f' +
           request.line1 + '\x1f' + request.line2;
}

// The label shown before anything has been previewed.
//
// A real render from lipgloss rather than something drawn here, so it shows
// exactly what the app produces and the QR code actually scans. The SKU is not
// arbitrary.
constexpr const char* kExampleStyle = "label_1_line_qr";
constexpr const char* kExampleSku = "PTN-120607";
constexpr const char* kExampleLine1 = "Super Graphic Ultra Modern Interface";

void submitExamplePreview(lipgloss::Client& client) {
    lipgloss::PrintRequest request;
    request.style = kExampleStyle;
    request.sku = kExampleSku;
    request.line1 = kExampleLine1;

    client.submitPreview(request);

    g_print.previewIsExample = true;
    g_print.exampleRequested = true;

    // Left empty so the first real refresh cannot mistake the example for a
    // preview of the form and skip itself.
    g_print.previewOf.clear();
}

// Asks for whatever the form currently describes, and records what it was
// asked for. Both modes go through here so every caller stays consistent.
void submitPreview(lipgloss::Client& client, claws::Client& claws,
                   const Style& style) {
    if (g_print.rangeMode) {
        // Only the first label of the run: they differ solely by SKU, so one
        // is representative and asking for all of them would be silly.
        //
        // The text is the caller's and identical across the run, so the only
        // thing standing in for the rest of the range is the first SKU.
        const std::string sku = cleanSku(g_print.rangeFrom);

        lipgloss::PrintRequest request;
        request.style = rangeStyle(style);
        request.sku = sku;
        request.line2 = style.usesLine2 ? g_print.line2 : "";

        if (style.needsLine1) {
            request.line1 = rangeName(claws, sku);
        }

        client.submitPreview(request);
    } else {
        client.submitPreview(buildRequest(style));
    }

    g_print.previewOf = requestSignature(style);
    g_print.previewIsExample = false;
}

// The preview's texture and the serial it was uploaded from.
//
// Owned by runSgumi, deliberately. This started out as a function-local static
// and segfaulted on every exit: a static is destroyed by __cxa_finalize_ranges
// during exit(), which is long after SDL_DestroyGPUDevice has run, and
// releasing a texture against a destroyed device dereferences null. Holding it
// in the frame loop's scope means it can be reset while the device is still
// alive -- see the shutdown sequence.
struct PreviewPanel {
    image::Texture texture;
    unsigned long long uploaded = 0;

    // The picked image, held apart from the label above. Two textures rather
    // than one because the two have independent serials, and sharing would mean
    // re-uploading whichever was displaced every time the style changed.
    image::Texture image;
    unsigned long long imageUploaded = 0;
};

// Sits above the form, always present, so the label being described is the
// first thing on screen rather than something found by scrolling.
//
// Capped rather than filling the row: lipgloss returns the label already
// scaled up for a screen, and letting that set the window's width would make
// the whole app as wide as a 960px preview for no benefit.
constexpr float kPreviewMaxWidth = 420.0f;

// And a cap the other way, which only an image label ever reaches. A rendered
// label is three times wider than it is tall, but an image fills the whole
// 96x320 stock, so magnifying it for a screen makes it tall rather than wide --
// far enough to push the entire form off the bottom if nothing said otherwise.
constexpr float kPreviewMaxHeight = 420.0f;

// A label-shaped blank, drawn rather than fetched.
//
// Gives the panel something to hold before anything has been asked of
// lipgloss, and -- because it takes the width and proportions a real preview
// would -- stops the whole form jumping down the screen the first time one
// arrives.
void drawPlaceholderLabel(float width) {
    // The Niimbot label's own proportions, 320x96. lipgloss adds a one pixel
    // frame on each side, which at this size is not worth reproducing.
    const ImVec2 size(width, width * (96.0f / 320.0f));
    const ImVec2 pos = ImGui::GetCursorScreenPos();
    const ImVec2 end(pos.x + size.x, pos.y + size.y);

    ImDrawList* draw = ImGui::GetWindowDrawList();

    // Paper, with the same frame lipgloss draws round its previews, so this
    // reads as a blank label rather than as a failed image.
    draw->AddRectFilled(pos, end, IM_COL32(237, 234, 228, 255));
    draw->AddRect(pos, end, IM_COL32(20, 22, 27, 255));

    const char* text = "No preview yet";
    const ImVec2 textSize = ImGui::CalcTextSize(text);

    draw->AddText(
        ImVec2(pos.x + (size.x - textSize.x) * 0.5f,
               pos.y + (size.y - textSize.y) * 0.5f),
        IM_COL32(120, 125, 135, 255),
        text);

    // The drawing above is free-floating, so the layout still has to be told
    // how much room it took.
    ImGui::Dummy(size);
}

// The image style's preview, which is not fetched from anywhere: what is drawn
// here is the very bitmap that is about to be uploaded, so it cannot drift from
// what prints the way a rendered preview could.
void drawImagePreview(
    const lipgloss::PreviewResult& preview,
    SDL_GPUDevice* device,
    PreviewPanel& panel)
{
    const bool dithered = !preview.png.empty();

    if (dithered && preview.serial != panel.uploaded) {
        panel.uploaded = preview.serial;
        panel.texture.load(device, preview.png.data(), preview.png.size());
    }

    if (!dithered && g_image.serial != panel.imageUploaded) {
        panel.imageUploaded = g_image.serial;

        if (g_image.png.empty()) {
            panel.image.reset();
        } else {
            panel.image.load(device, g_image.png.data(), g_image.png.size());
        }
    }

    image::Texture& texture = dithered ? panel.texture : panel.image;
    const float limit =
        std::min(ImGui::GetContentRegionAvail().x, kPreviewMaxWidth);

    if (!texture.valid()) {
        drawPlaceholderLabel(limit);
    } else {
        const float naturalW = static_cast<float>(texture.width());
        const float naturalH = static_cast<float>(texture.height());

        // Only ever shrinks, like the label preview. lipgloss has already
        // magnified the dither with nearest neighbour, and enlarging it again
        // here with a smooth sampler would average the dots back into grey --
        // which is the one thing this preview exists to avoid.
        //
        // Bounded both ways, since a label 320 long is the tall one.
        const float scale = std::min(
            1.0f,
            std::min(naturalW > 0.0f ? limit / naturalW : 1.0f,
                     naturalH > 0.0f ? kPreviewMaxHeight / naturalH : 1.0f));

        ImGui::Image(texture.id(), ImVec2(naturalW * scale, naturalH * scale));
    }

    if (!g_image.error.empty()) {
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextColored(kBad, "%s", g_image.error.c_str());
        ImGui::PopTextWrapPos();
        return;
    }

    if (!g_image.prepared.valid()) {
        return;
    }

    if (preview.state == lipgloss::PreviewResult::State::Pending) {
        ImGui::TextDisabled("Dithering...");
    } else if (!preview.error.empty()) {
        // Named rather than passed over. The undithered image on screen looks
        // better than the label is going to, so letting it stand unlabelled
        // would be the preview telling a comfortable lie.
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextColored(kWarn, "Not dithered: %s", preview.error.c_str());
        ImGui::PopTextWrapPos();
    } else if (dithered) {
        ImGui::TextDisabled("As the printer will dither it. Prints at %d x %d.",
                            g_image.prepared.width, g_image.prepared.height);
    }
}

void drawPreview(
    const lipgloss::PreviewResult& preview,
    const Style& style,
    SDL_GPUDevice* device,
    PreviewPanel& panel)
{
    if (style.isImage) {
        drawImagePreview(preview, device, panel);
        return;
    }

    image::Texture& texture = panel.texture;

    // Uploaded only when the bytes actually change, rather than decoding the
    // same PNG every frame.
    if (preview.serial != panel.uploaded) {
        panel.uploaded = preview.serial;

        if (preview.png.empty()) {
            texture.reset();
        } else {
            texture.load(device, preview.png.data(), preview.png.size());
        }
    }

    // The same width either way, so swapping a blank for a real label moves
    // nothing else on screen.
    const float limit =
        std::min(ImGui::GetContentRegionAvail().x, kPreviewMaxWidth);

    if (texture.valid()) {
        const float natural = static_cast<float>(texture.width());

        // Only ever shrinks: the source is already blown up for a screen, and
        // enlarging it further would just blur it. Aspect ratio preserved so a
        // barcode is never stretched into something that would not scan.
        const float scale = natural > limit ? limit / natural : 1.0f;

        ImGui::Image(
            texture.id(),
            ImVec2(natural * scale,
                   static_cast<float>(texture.height()) * scale));
    } else {
        drawPlaceholderLabel(limit);
    }

    if (preview.state == lipgloss::PreviewResult::State::Pending) {
        ImGui::TextDisabled("Rendering...");
    } else if (!preview.error.empty()) {
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextColored(kBad, "%s", preview.error.c_str());
        ImGui::PopTextWrapPos();
    } else if (g_print.previewIsExample) {
        ImGui::TextDisabled("Example label.");
    } else if (texture.valid() &&
               g_print.previewOf != requestSignature(style)) {
        // A picture of fields that have since been edited, which is worth
        // saying rather than letting someone print something they did not look
        // at. Rare now that the refresh is automatic -- it survives for the
        // cases the refresh declines, like lipgloss being unreachable.
        ImGui::TextColored(kWarn, "Fields changed since this preview.");
    }
}

// The image style's fields, in place of style/SKU/lines. Returns true when
// something changed that the prepared bitmap depends on.
bool drawImageFields(SDL_Window* window, int maxWidth) {
    bool changed = false;

    fieldLabel("Image");

    // Read once for the whole function. The dialog runs on its own thread, so
    // without this the button could be drawn enabled and the label beside it
    // drawn as though a dialog were open, from the same frame.
    bool waiting = false;

    {
        std::lock_guard<std::mutex> lock(g_picked.mutex);
        waiting = g_picked.waiting;
    }

    ImGui::BeginDisabled(waiting);

    if (ImGui::Button("Choose image...")) {
        // The filters are what stb_image can decode, so nothing offered here
        // can fail for being the wrong kind of file. The platform is free to
        // ignore them, which is why decodeFile still reports its own errors.
        static const SDL_DialogFileFilter kFilters[] = {
            { "Images", "png;jpg;jpeg;bmp;gif;tga;psd" },
            { "All files", "*" },
        };

        {
            std::lock_guard<std::mutex> lock(g_picked.mutex);
            g_picked.waiting = true;
        }

        SDL_ShowOpenFileDialog(onFilePicked, &g_picked, window, kFilters,
                               SDL_arraysize(kFilters), nullptr, false);
    }

    ImGui::EndDisabled();
    ImGui::SameLine();

    if (waiting) {
        ImGui::TextDisabled("Choosing...");
    } else if (!g_image.name.empty()) {
        ImGui::TextUnformatted(g_image.name.c_str());
    } else {
        ImGui::TextDisabled("Nothing chosen.");
    }

    if (g_image.source.valid()) {
        fieldLabel(" ");
        ImGui::TextDisabled("%d x %d in the file.",
                            g_image.source.width, g_image.source.height);
    }

    // Measured before either label moves the cursor, and used for both fields.
    const float pairWidth = halfFieldWidth();

    fieldLabel("Turn");
    ImGui::SetNextItemWidth(pairWidth);

    // Clockwise, and named in degrees rather than in turns, because the label
    // on the roll is what someone is picturing and it is not measured in turns.
    static const char* kTurns[] = { "None", "90\xc2\xb0", "180\xc2\xb0", "270\xc2\xb0" };

    if (ImGui::Combo("##turn", &g_image.quarterTurns, kTurns, 4)) {
        changed = true;
    }

    ImGui::SameLine();
    fieldLabel("Quantity");
    ImGui::SetNextItemWidth(pairWidth);
    ImGui::InputInt("##imageCopies", &g_image.copies);
    g_image.copies = std::clamp(g_image.copies, 1, lipgloss::kMaxCopies);

    fieldLabel("Fill");

    if (ImGui::Checkbox("Stretch to fill the label", &g_image.stretch)) {
        changed = true;
    }

    if (ImGui::IsItemHovered()) {
        ImGui::SetTooltip(
            "The label is precut at 12x40mm, which is %d by %d pixels, and it "
            "prints whole either way.\n\n"
            "On, the image is resized to exactly that, so it covers the label "
            "but its proportions go with it -- a square picture comes out over "
            "three times taller than it is wide.\n\n"
            "Off, it keeps its proportions, sits as large as fits, and the rest "
            "of the label is left white. Turn is what decides which of its "
            "sides runs along the long way.",
            kLabelPrintWidth, kLabelPrintLength);
    }

    fieldLabel("Text");
    ImGui::SetNextItemWidth(fieldWidth());
    ImGui::InputText("##imageDescription", g_image.description,
                     sizeof(g_image.description));

    if (ImGui::IsItemHovered()) {
        ImGui::SetTooltip(
            "What the queue calls this job. It is not printed on the label -- "
            "the image is the whole label. lipgloss keeps the first %d "
            "characters.",
            lipgloss::kMaxImageDescription);
    }

    return changed;
}

void drawPrint(
    lipgloss::Client& lipglossClient,
    claws::Client& clawsClient,
    const lipgloss::Snapshot& snapshot,
    SDL_Window* window)
{
    // Drained here rather than in the image branch below, so a file chosen just
    // before the style was changed is not left sitting in the handover.
    {
        std::string picked;
        std::string error;

        {
            std::lock_guard<std::mutex> lock(g_picked.mutex);

            if (g_picked.ready) {
                g_picked.ready = false;
                picked = g_picked.path;
                error = g_picked.error;
                g_picked.path.clear();
                g_picked.error.clear();
            }
        }

        if (!error.empty()) {
            g_image.error = "The file picker failed: " + error;
        } else if (!picked.empty()) {
            loadImage(picked, maxLabelWidth(snapshot.model));
        }
    }

    // Set by anything that changes how the label looks. Checked once at the
    // end, where the request is known to be valid, so the preview keeps up
    // without the button being pressed.
    //
    // Deliberately not per keystroke: IsItemDeactivatedAfterEdit fires when a
    // field is left after being changed, which is one request per field rather
    // than one per character.
    bool changed = false;

    const int maxWidth = maxLabelWidth(snapshot.model);

    // ---- Style, and quantity beside it when there is one ----
    //
    // The image style puts its own quantity box further down, beside the turn
    // control, so the style combo takes the whole row here the way it does for
    // a range.
    //
    // Measured before the label moves the cursor, and shared with the quantity
    // box beside it -- see halfFieldWidth.
    const float stylePairWidth = halfFieldWidth();

    fieldLabel("Style");
    ImGui::SetNextItemWidth(
        g_print.rangeMode || kStyles[g_print.styleIndex].isImage
            ? fieldWidth()
            : stylePairWidth);

    if (ImGui::BeginCombo("##style", kStyles[g_print.styleIndex].label)) {
        for (int i = 0; i < kStyleCount; ++i) {
            // A range prints one label per SKU, so a style that renders no SKU
            // would produce the same label every time. lipgloss refuses those
            // outright; leaving them out of the list is how that is said before
            // the round trip rather than after it.
            if (g_print.rangeMode && !kStyles[i].needsSku) {
                continue;
            }

            const bool selected = i == g_print.styleIndex;

            if (ImGui::Selectable(kStyles[i].label, selected)) {
                // Crossing between a rendered label and an image changes which
                // endpoint fills the one preview slot, so what is in it now
                // describes the wrong thing entirely rather than merely being
                // out of date. Dropped, and the image asked for again.
                if (kStyles[i].isImage != kStyles[g_print.styleIndex].isImage) {
                    lipglossClient.clearPreview();
                    g_print.previewOf.clear();
                    g_image.previewedSerial = 0;
                }

                g_print.styleIndex = i;
                changed = true;
            }

            if (selected) {
                ImGui::SetItemDefaultFocus();
            }
        }

        ImGui::EndCombo();
    }

    // Bound *after* the combo, not before it. A reference taken first would
    // still point at the previous style once the combo had changed the index,
    // so the automatic refresh would re-render the old label, find the
    // signature unchanged, and decide there was nothing to do -- which is
    // exactly why changing the style used to leave the preview alone.
    const Style& style = kStyles[g_print.styleIndex];

    if (!g_print.rangeMode && !style.isImage) {
        ImGui::SameLine();
        fieldLabel("Quantity");
        ImGui::SetNextItemWidth(stylePairWidth);
        ImGui::InputInt("##copies", &g_print.copies);
        g_print.copies = std::clamp(g_print.copies, 1, lipgloss::kMaxCopies);
    }

    if (style.isImage) {
        // ---- A picture, in place of everything a rendered label needs ----
        //
        // Rebuilt here rather than inside the fields, so one frame's worth of
        // changes costs one resample no matter how many of them there were.
        if (drawImageFields(window, maxWidth)) {
            g_image.dirty = true;
        }

        if (g_image.dirty) {
            prepareImage(maxWidth);
        }
    } else if (g_print.rangeMode) {
        // ---- A range of SKUs, one label each ----
        fieldLabel("From SKU");
        ImGui::SetNextItemWidth(fieldWidth());
        ImGui::InputText("##rangeFrom", g_print.rangeFrom,
                         sizeof(g_print.rangeFrom));
        changed = changed || ImGui::IsItemDeactivatedAfterEdit();

        fieldLabel("To SKU");
        ImGui::SetNextItemWidth(fieldWidth());
        ImGui::InputText("##rangeTo", g_print.rangeTo, sizeof(g_print.rangeTo));

        // Not folded into `changed`: the last SKU does not alter the first
        // label, which is all the preview ever shows for a range.

        // Text, when the style has somewhere to put it, comes from claws --
        // one name per SKU rather than one string for the run. That is the
        // point of range printing: add a batch of items, then label them all
        // without visiting each SKU by hand.
        //
        // Nothing is drawn when the style has no text cell, so picking Barcode
        // keeps the range form down to the three rows it needs.
        if (style.needsLine1) {
            clawsClient.fetchCatalog();

            const claws::Catalog catalog = clawsClient.catalog();

            switch (catalog.state) {
            case claws::Catalog::State::Idle:
            case claws::Catalog::State::Pending:
                ImGui::TextDisabled("Loading item names from claws...");
                break;

            case claws::Catalog::State::Failed:
                ImGui::PushTextWrapPos(0.0f);
                ImGui::TextColored(kBad, "%s", catalog.error.c_str());
                ImGui::PopTextWrapPos();
                break;

            case claws::Catalog::State::Ready: {
                // How much of the range claws actually knows. A gap is worth
                // seeing before printing rather than after: it means a SKU in
                // the span was never added, and that label will fall back to
                // showing its own SKU.
                int known = 0;
                int total = 0;

                int from = 0;
                int to = 0;

                if (skuNumber(g_print.rangeFrom, from) &&
                    skuNumber(g_print.rangeTo, to) && to >= from) {
                    for (int n = from; n <= to; ++n) {
                        ++total;

                        if (catalog.names.count(skuForNumber(n))) {
                            ++known;
                        }
                    }
                }

                if (total == 0) {
                    ImGui::TextDisabled("%zu items known to claws.",
                                        catalog.names.size());
                } else if (known == total) {
                    ImGui::TextColored(kGood, "%d of %d named by claws.",
                                       known, total);
                } else {
                    ImGui::TextColored(
                        kWarn,
                        "%d of %d named by claws; the rest will show their SKU.",
                        known, total);
                }

                break;
            }
            }
        }
    } else {
        // ---- SKU, with the claws toggle beside it ----
        //
        // The box is enabled whenever the SKU is of use to anything: either the
        // style puts it on the label, or claws is being asked to name the item.
        // A "Label" carries no SKU but still needs one typed here to look the
        // name up, which is the whole point of the toggle.
        const bool skuWanted = style.needsSku || g_print.useSkuName;

        fieldLabel("SKU");
        ImGui::BeginDisabled(!skuWanted);
        ImGui::SetNextItemWidth(halfFieldWidth());

        // EnterReturnsTrue for the barcode scanner, which types a SKU and
        // presses enter. IsItemDeactivatedAfterEdit catches the mouse case,
        // someone typing and then clicking away. Together they mean "the SKU
        // is finished", which is when a lookup is worth making; firing per
        // keystroke would ask claws about every prefix.
        const bool entered = ImGui::InputText(
            "##sku", g_print.sku, sizeof(g_print.sku),
            ImGuiInputTextFlags_EnterReturnsTrue);

        const bool committed = entered || ImGui::IsItemDeactivatedAfterEdit();

        // Only matters for styles that put the SKU on the label. When claws is
        // filling line 1 this usually fires twice -- once for the new SKU, once
        // when the name lands -- but the client holds a single pending preview,
        // so the two collapse into one request more often than not.
        changed = changed || committed;

        ImGui::EndDisabled();

        ImGui::SameLine();

        // Never disabled, even for a style with no SKU on it: switching this on
        // is what makes the SKU box above usable in the first place.
        const bool toggled = ImGui::Checkbox("From claws", &g_print.useSkuName);

        if (ImGui::IsItemHovered()) {
            ImGui::SetTooltip(
                "Fill line 1 with the item's name from claws.");
        }

        if (g_print.useSkuName) {
            if ((toggled || committed) && !blank(g_print.sku)) {
                g_print.filledFromSku.clear();
                clawsClient.lookup(cleanSku(g_print.sku));
            }
        } else if (toggled) {
            // Switched off: line 1 goes back to being the user's, keeping
            // whatever the last lookup put there rather than blanking it.
            clawsClient.clearLookup();
            g_print.filledFromSku.clear();
        }

        // ---- The two text lines ----
        //
        // Read after the lookup above so a freshly asked SKU shows "Looking
        // up" this frame rather than next.
        const claws::Lookup lookup = clawsClient.lookupResult();

        // Locked only while claws is actually supplying it. An editable field
        // something else rewrites is a trap, but so is a locked empty one: with
        // the toggle on and a SKU claws has never heard of, line 1 would
        // otherwise be both empty and uneditable, leaving Print blocked on
        // "needs line 1" with no way out.
        const bool line1Derived =
            g_print.useSkuName && lookup.state == claws::Lookup::State::Found;

        fieldLabel("Line 1");
        ImGui::BeginDisabled(!style.needsLine1 || line1Derived);
        ImGui::SetNextItemWidth(halfFieldWidth());
        ImGui::InputText("##line1", g_print.line1, sizeof(g_print.line1));
        changed = changed || ImGui::IsItemDeactivatedAfterEdit();
        ImGui::EndDisabled();

        ImGui::SameLine();

        fieldLabel("Line 2");
        ImGui::BeginDisabled(!style.usesLine2);
        ImGui::SetNextItemWidth(halfFieldWidth());
        ImGui::InputText("##line2", g_print.line2, sizeof(g_print.line2));
        changed = changed || ImGui::IsItemDeactivatedAfterEdit();
        ImGui::EndDisabled();

        if (g_print.useSkuName) {
            // A lookup landing rewrites line 1, and that is a change to the
            // label as much as typing one would be -- without this the preview
            // would still show whatever line 1 held before claws answered.
            const std::string filledBefore = g_print.filledFromSku;
            drawLookupResult(lookup);
            changed = changed || g_print.filledFromSku != filledBefore;
        }
    }

    // ---- The mode switch ----
    //
    // Hidden for the image style rather than greyed: a range is one label per
    // SKU and an image has no SKU, so the two have nothing to say to each
    // other. Selecting the image style from inside a range is already
    // impossible, since the list only offers styles that render a SKU.
    ImGui::BeginDisabled(style.isImage);

    if (ImGui::Checkbox("Range print", &g_print.rangeMode)) {
        // The style list shrinks on the way in, so a selection that is no
        // longer offered has to move -- otherwise the combo would show a style
        // that cannot be picked again once left.
        if (g_print.rangeMode && !kStyles[g_print.styleIndex].needsSku) {
            for (int i = 0; i < kStyleCount; ++i) {
                if (kStyles[i].needsSku) {
                    g_print.styleIndex = i;
                    break;
                }
            }
        }

        // The preview on screen describes the other mode's fields, so it is
        // dropped back to the blank rather than left showing the wrong label.
        lipglossClient.clearPreview();
        g_print.previewOf.clear();
        changed = true;
    }

    if (ImGui::IsItemHovered()) {
        ImGui::SetTooltip("Print one label per SKU across a range.");
    }

    ImGui::EndDisabled();
    ImGui::Spacing();

    // ---- What can be pressed, and why not ----
    const lipgloss::ActionResult action = lipglossClient.actionResult();
    const bool pending = action.state == lipgloss::ActionResult::State::Pending;

    int rangeFrom = 0;
    int rangeTo = 0;
    const char* blocker = nullptr;

    if (style.isImage) {
        blocker = imageBlocker();
    } else if (g_print.rangeMode) {
        if (!skuNumber(g_print.rangeFrom, rangeFrom)) {
            blocker = "Enter a starting SKU.";
        } else if (!skuNumber(g_print.rangeTo, rangeTo)) {
            blocker = "Enter an ending SKU.";
        } else if (rangeTo < rangeFrom) {
            blocker = "The first SKU is higher than the last.";
        }
    } else {
        blocker = printBlocker(style);
    }

    const bool disabled = pending || blocker != nullptr || !snapshot.reachable;

    // Automatic refresh.
    //
    // The change is remembered rather than acted on immediately, because the
    // moment something changes is often a moment the request is not yet
    // printable: picking a style and then filling in the range leaves every
    // edit blocked on a field that has not been typed yet. Dropping `changed`
    // on those frames meant the last field completed the form but triggered
    // nothing, and the preview sat on the previous style forever.
    //
    // So the flag survives until it can be honoured -- which also covers
    // editing while lipgloss is down and having it catch up once it returns.
    if (changed) {
        g_print.previewStale = true;
    }

    // The image style has its own trigger: a prepared bitmap, rather than a
    // form that has stopped being edited. Not gated on `blocker` either, since
    // an image too wide to print is still worth seeing dithered -- that is how
    // someone decides whether to turn it or shrink it.
    if (style.isImage && snapshot.reachable &&
        g_image.prepared.valid() && !g_image.png.empty() &&
        g_image.serial != g_image.previewedSerial) {
        g_image.previewedSerial = g_image.serial;

        // Magnified as far as fits the panel and no further. Asking for more
        // and shrinking it here would only hand the GPU's smooth sampler a
        // dither to average away, so the magnification is chosen to be the one
        // that gets displayed.
        //
        // Measured against the shape it comes back in, which is the label
        // turned for reading: the 320 runs across, not down.
        const int shownWidth = std::max(g_image.prepared.height, 1);
        const int shownHeight = std::max(g_image.prepared.width, 1);

        const int scale = std::clamp(
            std::min(static_cast<int>(kPreviewMaxWidth) / shownWidth,
                     static_cast<int>(kPreviewMaxHeight) / shownHeight),
            1,
            lipgloss::kMaxPreviewScale);

        lipglossClient.submitImagePreview(g_image.png, scale,
                                          lipgloss::kPreviewRotate);
    }

    if (g_print.previewStale && !style.isImage && blocker == nullptr &&
        snapshot.reachable) {
        // Still checked against what is showing: completing a form without
        // having altered what the label says asks lipgloss for nothing.
        if (requestSignature(style) != g_print.previewOf) {
            submitPreview(lipglossClient, clawsClient, style);
        }

        g_print.previewStale = false;
    }

    ImGui::BeginDisabled(disabled);

    if (ImGui::Button("Print")) {
        if (style.isImage) {
            lipgloss::ImageRequest request;
            request.png = g_image.png;
            request.copies = g_image.copies;

            // The filename when the box has been emptied, because a blank
            // Description column in the queue names nothing at all.
            request.description = blank(g_image.description)
                ? g_image.name
                : g_image.description;

            lipglossClient.submitImage(std::move(request));
        } else if (g_print.rangeMode) {
            // One entry per SKU in the span. Unknown SKUs are given their own
            // SKU as the text, so a gap in the range still prints a label that
            // identifies its bin rather than failing the whole job.
            std::map<std::string, std::string> names;

            if (style.needsLine1) {
                const claws::Catalog catalog = clawsClient.catalog();

                for (int n = rangeFrom; n <= rangeTo; ++n) {
                    const std::string sku = skuForNumber(n);
                    const auto it = catalog.names.find(sku);

                    names[sku] = it != catalog.names.end() && !it->second.empty()
                        ? it->second
                        : sku;
                }
            }

            lipglossClient.submitBarcodes(
                rangeFrom, rangeTo, rangeStyle(style),
                "", style.usesLine2 ? g_print.line2 : "",
                std::move(names));
        } else {
            lipglossClient.submitPrint(buildRequest(style));
        }
    }

    ImGui::EndDisabled();

    ImGui::SameLine();

    if (pending && isQueueAction(action.kind)) {
        // The client has one action in flight at a time, so a resume, clear or
        // cancel is holding the slot the Print button needs. Saying which is
        // better than "Sending..." for a request nobody made from here.
        ImGui::TextDisabled("Waiting on the queue...");
    } else if (pending) {
        ImGui::TextDisabled("Sending...");
    } else if (!snapshot.reachable) {
        ImGui::TextColored(kBad, "lipgloss is unreachable.");
    } else if (blocker) {
        ImGui::TextDisabled("%s", blocker);
    } else if (style.isImage && action.state == lipgloss::ActionResult::State::Idle) {
        // Both, because they answer different questions: the size says whether
        // the image fitted the way it was meant to, and the count is the one a
        // roll gets spent on.
        ImGui::TextDisabled("%d x %d, %d label%s.",
                            g_image.prepared.width, g_image.prepared.height,
                            g_image.copies, g_image.copies == 1 ? "" : "s");
    } else if (g_print.rangeMode) {
        const int total = rangeTo - rangeFrom + 1;
        ImGui::TextDisabled("%d label%s.", total, total == 1 ? "" : "s");
    } else if (action.state == lipgloss::ActionResult::State::Idle) {
        // A range has said this all along and an image now does too. A plain
        // label saying nothing meant the one mode where the quantity box is the
        // only thing between you and a hundred labels was also the one mode
        // that never repeated the number back.
        ImGui::TextDisabled("%d label%s.", g_print.copies,
                            g_print.copies == 1 ? "" : "s");
    } else if (isQueueAction(action.kind)) {
        // Reported under the queue, not here. Without this a resume would land
        // in the print status line as "Job -1 queued", and a cancel's outcome
        // would read as a print that failed.
    } else {
        switch (action.state) {
        case lipgloss::ActionResult::State::Ok:
            // A job accepted onto a paused queue is not printing, and saying
            // "queued" without that would be misleading.
            if (action.queuePaused) {
                ImGui::TextColored(kWarn, "Job %lld queued, queue is paused.",
                                   action.jobId);
            } else {
                ImGui::TextColored(kGood, "Job %lld queued.", action.jobId);
            }
            break;

        case lipgloss::ActionResult::State::Failed:
            ImGui::PushTextWrapPos(0.0f);
            ImGui::TextColored(kBad, "%s", action.message.c_str());
            ImGui::PopTextWrapPos();
            break;

        default:
            break;
        }
    }

    // Range mode puts its outcome on its own line rather than beside the
    // button, because the line above it is already the coverage count.
    if (g_print.rangeMode && !isQueueAction(action.kind)) {
        if (action.state == lipgloss::ActionResult::State::Ok) {
            ImGui::TextColored(kGood, "Job %lld queued.", action.jobId);
        } else if (action.state == lipgloss::ActionResult::State::Failed) {
            ImGui::PushTextWrapPos(0.0f);
            ImGui::TextColored(kBad, "%s", action.message.c_str());
            ImGui::PopTextWrapPos();
        }
    }
}

// The job id a row carries, or false when it does not carry one. It arrives as
// text because the same rows feed illusion's terminal table, and DELETE
// /queue/{id} wants the number back.
bool jobNumber(const std::string& text, long long& out) {
    if (text.empty() ||
        text.find_first_not_of("0123456789") != std::string::npos) {
        return false;
    }

    // strtoll rather than stoll, for the same reason skuNumber uses strtol: a
    // number too long to fit is a return value here, not an exception.
    errno = 0;
    const long long parsed = std::strtoll(text.c_str(), nullptr, 10);

    if (errno != 0 || parsed < 0) {
        return false;
    }

    out = parsed;
    return true;
}

// What is in flight, for the line beside the queue buttons. A print can hold
// the slot too, and saying so explains why the buttons are greyed.
const char* pendingQueueLabel(lipgloss::ActionResult::Kind kind) {
    switch (kind) {
    case lipgloss::ActionResult::Kind::Resume:
        return "Resuming...";
    case lipgloss::ActionResult::Kind::Clear:
        return "Clearing...";
    case lipgloss::ActionResult::Kind::Cancel:
        return "Cancelling...";
    default:
        return "Sending a print...";
    }
}

// How the outcome of a queue action reads. Every one of these can come back 200
// having changed nothing, so the colour is taken from what lipgloss says it
// did rather than from the fact that it answered at all.
ImVec4 queueActionColour(const lipgloss::ActionResult& action,
                         const lipgloss::Snapshot& snapshot) {
    if (action.state == lipgloss::ActionResult::State::Failed) {
        return kBad;
    }

    switch (action.kind) {
    case lipgloss::ActionResult::Kind::Resume:
        // Whether the printer actually came back is in the next poll. Until
        // the queue reports itself unpaused, this stays a warning.
        return snapshot.paused ? kWarn : kGood;

    case lipgloss::ActionResult::Kind::Cancel:
        // Not cancelled is the ordinary answer for a job that finished between
        // the click and the request, so it is a note rather than a failure.
        return action.cancelled ? kGood : kWarn;

    default:
        return kGood;
    }
}

constexpr const char* kClearPopup = "Clear the print queue?";

// Clearing cannot be undone on the far end, and the queue is shared -- a run
// the bot started is as easy to throw away as your own. The counts are what
// make that visible while there is still time to not do it.
void drawClearConfirm(const lipgloss::Snapshot& snapshot,
                      lipgloss::Client& client) {
    if (!ImGui::BeginPopupModal(kClearPopup, nullptr,
                                ImGuiWindowFlags_AlwaysAutoResize)) {
        return;
    }

    // The queue drained while the question was on screen. There is nothing left
    // to confirm, so it stops being asked rather than clearing an empty queue.
    if (snapshot.jobs.empty()) {
        ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
        return;
    }

    ImGui::Text("%d job(s) and %d label(s) will never print.",
                snapshot.pendingJobs, snapshot.pendingLabels);

    ImGui::Spacing();

    if (ImGui::Button("Clear the queue")) {
        client.submitClear();
        ImGui::CloseCurrentPopup();
    }

    ImGui::SameLine();

    if (ImGui::Button("Keep them")) {
        ImGui::CloseCurrentPopup();
    }

    ImGui::EndPopup();
}

void drawQueue(const lipgloss::Snapshot& snapshot, lipgloss::Client& client) {
    if (!snapshot.reachable || snapshot.unauthorized) {
        ImGui::TextDisabled("Queue unavailable.");

        // The indicator in the bar has room for three words, so the reason goes here
        if (!snapshot.error.empty()) {
            ImGui::Spacing();
            ImGui::PushTextWrapPos(0.0f);
            ImGui::TextColored(snapshot.unauthorized ? kWarn : kBad, "%s",
                               snapshot.error.c_str());
            ImGui::PopTextWrapPos();
        }

        return;
    }

    if (!snapshot.title.empty()) {
        ImGui::TextUnformatted(snapshot.title.c_str());
    }

    if (!snapshot.description.empty()) {
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextDisabled("%s", snapshot.description.c_str());
        ImGui::PopTextWrapPos();
    }

    // Resume, clear and cancel share the client's one action slot, so only one
    // can be in flight and one line below reports whichever it was.
    const lipgloss::ActionResult action = client.actionResult();
    const bool pending =
        action.state == lipgloss::ActionResult::State::Pending;
    const bool mine = isQueueAction(action.kind);

    ImGui::Spacing();

    // Resume only while paused. lipgloss stops the queue when the printer needs
    // attention -- out of labels, lid open, unplugged -- and nothing starts
    // printing again until someone says the problem is dealt with. The reason
    // is in the description above, so this is just the acknowledgement.
    if (snapshot.paused) {
        ImGui::BeginDisabled(pending);

        if (ImGui::Button("Resume queue")) {
            client.submitResume();
        }

        ImGui::EndDisabled();
        ImGui::SameLine();
    }

    // Clearing an empty queue is answered politely rather than refused, but
    // offering the button anyway invites a click that can only be a mistake.
    ImGui::BeginDisabled(pending || snapshot.jobs.empty());

    if (ImGui::Button("Clear queue")) {
        ImGui::OpenPopup(kClearPopup);
    }

    ImGui::EndDisabled();

    // Outside the BeginDisabled above, or the modal's own buttons would inherit
    // the disabled state from the button that opened it.
    drawClearConfirm(snapshot, client);

    if (pending) {
        ImGui::SameLine();
        ImGui::TextDisabled("%s", pendingQueueLabel(action.kind));
    } else if (mine && action.state != lipgloss::ActionResult::State::Idle) {
        // lipgloss's own wording, verbatim. It answers 200 even when it could
        // not do the thing -- "still unable to print, the queue is staying
        // paused" is a successful request with an unsuccessful outcome -- so
        // the message is the only account of what happened, and the colour
        // comes from the state it left behind rather than from the status code.
        ImGui::Spacing();
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextColored(queueActionColour(action, snapshot), "%s",
                           action.message.c_str());
        ImGui::PopTextWrapPos();
    }

    if (snapshot.jobs.empty()) {
        return;
    }

    ImGui::Spacing();

    constexpr ImGuiTableFlags kFlags =
        ImGuiTableFlags_Borders |
        ImGuiTableFlags_RowBg |
        ImGuiTableFlags_SizingStretchProp |
        ImGuiTableFlags_ScrollY;

    // Height is bounded so a hundred-job queue cannot push the window off the
    // screen; ScrollY above is what makes the overflow reachable.
    const float height = ImGui::GetTextLineHeightWithSpacing() * 12.0f;

    if (ImGui::BeginTable("jobs", 6, kFlags, ImVec2(0.0f, height))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Job", ImGuiTableColumnFlags_WidthFixed);
        ImGui::TableSetupColumn("Description");
        ImGui::TableSetupColumn("Labels", ImGuiTableColumnFlags_WidthFixed);
        ImGui::TableSetupColumn("Source", ImGuiTableColumnFlags_WidthFixed);
        ImGui::TableSetupColumn("State", ImGuiTableColumnFlags_WidthFixed);

        // No heading: a column of buttons labels itself, and "Cancel" over the
        // top would read as something that could be clicked.
        ImGui::TableSetupColumn("##cancel", ImGuiTableColumnFlags_WidthFixed |
                                                ImGuiTableColumnFlags_NoHeaderLabel);
        ImGui::TableHeadersRow();

        for (const lipgloss::Job& job : snapshot.jobs) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(job.jobId.c_str());
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(job.description.c_str());
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(job.labels.c_str());
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(job.source.c_str());
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(job.state.c_str());
            ImGui::TableNextColumn();

            long long id = 0;

            // The id arrives as text because the same rows feed a terminal
            // table. A row that does not carry a number is not one this can
            // address, so it simply gets no button rather than a broken one.
            if (!jobNumber(job.jobId, id)) {
                continue;
            }

            // The job id, not the row index: ImGui remembers state per id, and
            // rows shift up every time one finishes printing.
            ImGui::PushID(static_cast<int>(id));
            ImGui::BeginDisabled(pending);

            if (ImGui::SmallButton("Cancel")) {
                client.submitCancel(id);
            }

            ImGui::EndDisabled();
            ImGui::PopID();
        }

        ImGui::EndTable();
    }
}

int runSgumi() {
    SDL_SetAppMetadata("SGUMI", SGUMI_VERSION, kAppId);

    // Useful on Linux desktops, this is what DEs match against the
    // .desktop file to give the window its name and icon.
    SDL_SetHint(SDL_HINT_APP_ID, kAppId);

    // No gamepad subsystem, unlike the 2027 frontend: nothing here is flown.
    if (!SDL_Init(SDL_INIT_VIDEO | SDL_INIT_EVENTS)) {
        reportFatal("SDL_Init failed", SDL_GetError());
        return -1;
    }

    const SDL_WindowFlags windowFlags =
        SDL_WINDOW_RESIZABLE |
        SDL_WINDOW_HIGH_PIXEL_DENSITY;

    // Narrow and tall rather than square
    SDL_Window* window = SDL_CreateWindow("SGUMI", 620, 820, windowFlags);

    if (!window) {
        reportFatal("SDL_CreateWindow failed", SDL_GetError());
        SDL_Quit();
        return -1;
    }

    // No DXIL here. The 2027 frontend advertises it because its NV12 pipeline
    // ships precompiled DXIL blobs; SGUMI compiles no shaders of its own, and
    // ImGui's SDL_GPU backend carries whatever it needs for every backend.
    const SDL_GPUShaderFormat shaderFormats =
        SDL_GPU_SHADERFORMAT_SPIRV |
        SDL_GPU_SHADERFORMAT_MSL |
        SDL_GPU_SHADERFORMAT_DXIL;

    // The D3D12 debug layer needs the "Graphics Tools" optional Windows
    // feature installed; asking for it without that present fails device
    // creation outright.
#if defined(NDEBUG)
    const bool gpuDebugMode = false;
#else
    const bool gpuDebugMode = true;
#endif

    SDL_GPUDevice* gpuDevice =
        SDL_CreateGPUDevice(shaderFormats, gpuDebugMode, nullptr);

    if (!gpuDevice) {
        reportFatal("SDL_CreateGPUDevice failed", SDL_GetError());
        SDL_DestroyWindow(window);
        SDL_Quit();
        return -1;
    }

    if (!SDL_ClaimWindowForGPUDevice(gpuDevice, window)) {
        reportFatal("SDL_ClaimWindowForGPUDevice failed", SDL_GetError());
        SDL_DestroyGPUDevice(gpuDevice);
        SDL_DestroyWindow(window);
        SDL_Quit();
        return -1;
    }

    SDL_SetGPUSwapchainParameters(
        gpuDevice,
        window,
        SDL_GPU_SWAPCHAINCOMPOSITION_SDR,
        SDL_GPU_PRESENTMODE_VSYNC);

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGui::GetIO().IniFilename = paths::imguiIniPath();
    ImGui::StyleColorsDark();
    applyTheme();  // see src/Theme.cpp to tweak

    ImGui_ImplSDL3_InitForSDLGPU(window);

    ImGui_ImplSDLGPU3_InitInfo initInfo = {};
    initInfo.Device = gpuDevice;
    initInfo.ColorTargetFormat =
        SDL_GetGPUSwapchainTextureFormat(gpuDevice, window);
    initInfo.MSAASamples = SDL_GPU_SAMPLECOUNT_1;
    ImGui_ImplSDLGPU3_Init(&initInfo);

    const fs::path configPath = paths::defaultConfigPath();

    if (loadConfigFromFile(configPath, g_config)) {
        std::cout << "Loaded config file: " << configPath << std::endl;
    } else {
        std::cout << "Using default config. Config path: " << configPath
                  << std::endl;
    }

    lipgloss::Client client;
    client.setEndpoint(g_config.lipglossUrl, g_config.lipglossToken);
    client.setPollInterval(std::chrono::seconds(g_config.pollSeconds));
    client.start();

    claws::Client clawsClient;
    clawsClient.setEndpoint(g_config.clawsUrl, g_config.clawsToken);
    clawsClient.start();

    PreviewPanel previewPanel;

    bool showSettings = g_config.lipglossToken[0] == '\0';
#if defined(SGUMI_THEME_EDITOR)
    bool showThemeEditor = false;
#endif
    std::string configStatus;

    bool done = false;

    while (!done) {
        SDL_Event event;

        while (SDL_PollEvent(&event)) {
            ImGui_ImplSDL3_ProcessEvent(&event);

            if (event.type == SDL_EVENT_QUIT) {
                done = true;
            }

            if (event.type == SDL_EVENT_WINDOW_CLOSE_REQUESTED &&
                event.window.windowID == SDL_GetWindowID(window)) {
                done = true;
            }
        }

        if (SDL_GetWindowFlags(window) & SDL_WINDOW_MINIMIZED) {
            SDL_Delay(10);
            continue;
        }

        ImGui_ImplSDLGPU3_NewFrame();
        ImGui_ImplSDL3_NewFrame();
        ImGui::NewFrame();

#if defined(SGUMI_THEME_EDITOR)
        if (ImGui::IsKeyPressed(ImGuiKey_F10, false)) {
            showThemeEditor = !showThemeEditor;
        }
#endif

        // One snapshot per frame, taken once. Calling client.snapshot() from
        // each draw function would take the worker's lock several times and
        // could show two different polls in one frame.
        const lipgloss::Snapshot snapshot = client.snapshot();

        // The main window fills the OS window and is not movable
        const ImGuiViewport* viewport = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(viewport->WorkPos);
        ImGui::SetNextWindowSize(viewport->WorkSize);

        constexpr ImGuiWindowFlags kMainFlags =
            ImGuiWindowFlags_NoTitleBar |
            ImGuiWindowFlags_NoResize |
            ImGuiWindowFlags_NoMove |
            ImGuiWindowFlags_NoCollapse |
            ImGuiWindowFlags_NoBringToFrontOnFocus |
            ImGuiWindowFlags_MenuBar;

        if (ImGui::Begin("##main", nullptr, kMainFlags)) {
            if (ImGui::BeginMenuBar()) {
                if (ImGui::BeginMenu("SGUMI")) {
                    if (ImGui::MenuItem("Settings")) {
                        showSettings = true;
                    }

                    ImGui::EndMenu();
                }

                drawStatusBar(snapshot, client);

                ImGui::EndMenuBar();
            }

            const lipgloss::PreviewResult previewResult = client.previewResult();

            // Nothing has ever been previewed, so show a real label rather
            // than an empty frame.
            if (!g_print.exampleRequested &&
                previewResult.state == lipgloss::PreviewResult::State::Idle &&
                snapshot.reachable) {
                submitExamplePreview(client);
            }

            // The preview goes first: the label is what this screen is about,
            // and the fields below are how it gets changed.
            drawPreview(previewResult,
                        kStyles[g_print.styleIndex],
                        gpuDevice,
                        previewPanel);

            ImGui::Spacing();
            ImGui::SeparatorText("Print");
            drawPrint(client, clawsClient, snapshot, window);

            ImGui::Spacing();
            ImGui::SeparatorText("Print queue");
            drawQueue(snapshot, client);
        }

        ImGui::End();

        if (showSettings) {
            ImGui::SetNextWindowSize(ImVec2(750, 400), ImGuiCond_FirstUseEver);

            if (ImGui::Begin("Settings", &showSettings)) {
                // Shared by both service tabs rather than duplicated into
                // each. Apply pushes both endpoints, because the config file
                // holds both and saving half of it would be a strange thing
                // for a button labelled "save" to do.
                auto applyConfig = [&] {
                    client.setEndpoint(g_config.lipglossUrl,
                                       g_config.lipglossToken);
                    clawsClient.setEndpoint(g_config.clawsUrl,
                                            g_config.clawsToken);
                    client.setPollInterval(
                        std::chrono::seconds(g_config.pollSeconds));
                };

                auto drawApplyRow = [&] {
                    ImGui::Spacing();

                    if (ImGui::Button("Apply and save")) {
                        // Applied to the live clients either way: a token that
                        // works is worth having for this session even if the
                        // disk write failed.
                        applyConfig();

                        configStatus = saveConfigToFile(configPath, g_config)
                                        ? "Saved to " + configPath.string()
                                        : "Failed to write " + configPath.string();
                    }

                    ImGui::SameLine();

                    if (ImGui::Button("Apply without saving")) {
                        applyConfig();
                        configStatus = "Applied for this session only.";
                    }

                    if (!configStatus.empty()) {
                        ImGui::Spacing();
                        ImGui::PushTextWrapPos(0.0f);
                        ImGui::TextDisabled("%s", configStatus.c_str());
                        ImGui::PopTextWrapPos();
                    }
                };

                if (ImGui::BeginTabBar("Config Tabs")) {
                    if (ImGui::BeginTabItem("Lipgloss")) {
                        ImGui::TextDisabled("Must match lipgloss.yaml on the print server.");
                        ImGui::Spacing();

                        ImGui::InputText("lipgloss URL", g_config.lipglossUrl,
                                        sizeof(g_config.lipglossUrl));
                        ImGui::InputText("Token", g_config.lipglossToken,
                                        sizeof(g_config.lipglossToken),
                                        ImGuiInputTextFlags_Password);

                        ImGui::Spacing();

                        if (ImGui::InputInt("Queue poll (seconds)",
                                            &g_config.pollSeconds)) {
                            // Clamped as it is typed
                            g_config.pollSeconds = std::clamp(
                                g_config.pollSeconds,
                                kMinPollSeconds,
                                kMaxPollSeconds);
                        }

                        if (ImGui::IsItemHovered()) {
                            ImGui::SetTooltip(
                                "How often the queue is re-read.",
                                kMinPollSeconds);
                        }

                        drawApplyRow();

                        ImGui::EndTabItem();
                    }

                    if (ImGui::BeginTabItem("Claws")) {
                        ImGui::TextDisabled(
                            "Must match claws.yaml on the inventory host. "
                            "Only used to look a SKU up; SGUMI never writes to claws.");
                        ImGui::Spacing();

                        ImGui::InputText("claws URL", g_config.clawsUrl,
                                        sizeof(g_config.clawsUrl));
                        ImGui::InputText("Token", g_config.clawsToken,
                                        sizeof(g_config.clawsToken),
                                        ImGuiInputTextFlags_Password);

                        drawApplyRow();

                        ImGui::EndTabItem();
                    }

                    if (ImGui::BeginTabItem("About")) {
                        ImGui::TextUnformatted("Super Graphic Ultra Modern Interface");
                        ImGui::TextDisabled("illusion's frontend for lipgloss");

                        ImGui::SeparatorText("This build");
                        drawBuildInfo(gpuDevice);

                        ImGui::SeparatorText("lipgloss");
                        drawServiceInfo(snapshot, client.eventsConnected(),
                                        g_config.pollSeconds);

                        ImGui::SeparatorText("claws");
                        drawClawsInfo(clawsClient.snapshot());

                        ImGui::Spacing();
                        ImGui::Separator();
                        ImGui::TextDisabled("Config: %s", configPath.string().c_str());

                        ImGui::EndTabItem();
                    }
                    ImGui::EndTabBar();
                }
            }

            ImGui::End();
        }

#if defined(SGUMI_THEME_EDITOR)
        if (showThemeEditor) {
            drawThemeEditor(&showThemeEditor);
        }
#endif

        ImGui::Render();
        ImDrawData* drawData = ImGui::GetDrawData();

        SDL_GPUCommandBuffer* cmd = SDL_AcquireGPUCommandBuffer(gpuDevice);

        SDL_GPUTexture* swapchainTex = nullptr;
        SDL_WaitAndAcquireGPUSwapchainTexture(
            cmd, window, &swapchainTex, nullptr, nullptr);

        if (swapchainTex) {
            ImGui_ImplSDLGPU3_PrepareDrawData(drawData, cmd);

            SDL_GPUColorTargetInfo target = {};
            target.texture = swapchainTex;
            target.load_op = SDL_GPU_LOADOP_CLEAR;
            target.store_op = SDL_GPU_STOREOP_STORE;
            target.clear_color = SDL_FColor { 0.08f, 0.09f, 0.11f, 1.0f };

            SDL_GPURenderPass* pass =
                SDL_BeginGPURenderPass(cmd, &target, 1, nullptr);
            ImGui_ImplSDLGPU3_RenderDrawData(drawData, cmd, pass);
            SDL_EndGPURenderPass(pass);
        }

        SDL_SubmitGPUCommandBuffer(cmd);
    }

    // Before the GPU teardown below: the worker holds no GPU resources, but it
    // does hold a socket, and joining it here keeps shutdown ordered rather
    // than relying on the destructor firing at the right moment.
    client.stop();
    clawsClient.stop();

    SDL_WaitForGPUIdle(gpuDevice);

    // Before the device goes: SDL_ReleaseGPUTexture needs the device that made
    // the texture. Letting this run on its own at scope exit would put it
    // after SDL_DestroyGPUDevice below, which is a null dereference -- the
    // exact crash an earlier version of this shipped with.
    previewPanel.texture.reset();

    // Both, and before the device goes: releasing a texture needs the device
    // that made it, so one left to its destructor would be freed against a
    // dangling handle.
    previewPanel.image.reset();

    ImGui_ImplSDLGPU3_Shutdown();
    ImGui_ImplSDL3_Shutdown();
    ImGui::DestroyContext();

    SDL_ReleaseWindowFromGPUDevice(gpuDevice, window);
    SDL_DestroyGPUDevice(gpuDevice);
    SDL_DestroyWindow(window);
    SDL_Quit();

    return 0;
}

}  // namespace

// On Windows, SDL_main.h redefines main() to SDL_main and SDL supplies the
// real WinMain, which calls SDL_main(int, char**). The signature has to match
// exactly or the link fails, which is why argc/argv are named and unused
// rather than omitted.
int main(int argc, char** argv) {
    (void)argc;
    (void)argv;

    // Once, around everything. libcurl's global init is not thread-safe and
    // happens implicitly inside the first curl_easy_init() if nobody does it
    // first -- with two clients on two worker threads, that implicit init
    // would be a race. See Http.hpp.
    http::globalInit();

    const int result = runSgumi();

    http::globalCleanup();
    return result;
}
