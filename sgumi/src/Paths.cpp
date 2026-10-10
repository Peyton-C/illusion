// windows.h, for the known-folder lookup defaultConfigPath() does below. It is
// first in the file because both defines only bite if nothing has pulled
// windows.h in already: NOMINMAX because the min/max macros otherwise break
// std::clamp and std::min at their call sites, and WIN32_LEAN_AND_MEAN because
// it drops a pile of headers nothing here needs.
#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <shlobj.h>
#endif

#include "Paths.hpp"

#include <cstdlib>
#include <string>
#include <system_error>

namespace fs = std::filesystem;

namespace paths {
namespace {

constexpr const char* kConfigFileName = "sgumi_config.json";
constexpr const char* kImGuiFileName = "sgumi_imgui.ini";

#if defined(_WIN32)
// HOME does not exist on Windows; this is the equivalent.
//
// The returned string is wide, which is why this hands back an fs::path rather
// than a std::string. fs::path takes wchar_t natively; narrowing it first would
// mangle any non-ASCII user name, since std::filesystem reads a narrow string
// as the active code page rather than as UTF-8.
fs::path knownFolderPath(REFKNOWNFOLDERID folderId) {
    PWSTR raw = nullptr;

    const HRESULT result =
        SHGetKnownFolderPath(folderId, KF_FLAG_CREATE, nullptr, &raw);

    // Documented to need freeing even on failure.
    if (FAILED(result)) {
        CoTaskMemFree(raw);
        return {};
    }

    fs::path path(raw);
    CoTaskMemFree(raw);
    return path;
}
#endif

#if !defined(_WIN32) && !defined(__APPLE__)
// The XDG base directory the config file belongs under.
//
// $XDG_CONFIG_HOME when the session sets one, ~/.config when it does not. The
// spec defines the second as the *default value of the first*, not as an
// alternative to it, so hardcoding ~/.config quietly ignores every setup that
// moved it: a sandboxed Flatpak run, a home directory on a network mount, a
// user who keeps dotfiles elsewhere.
//
// The leading-slash test is from the spec too. A relative $XDG_CONFIG_HOME is
// defined to be invalid and must be ignored rather than resolved against the
// working directory -- which would land the config next to wherever the binary
// was launched from.
fs::path xdgConfigHome() {
    const char* configHome = std::getenv("XDG_CONFIG_HOME");

    if (configHome && configHome[0] == '/') {
        return fs::path(configHome);
    }

    const char* home = std::getenv("HOME");

    if (!home || home[0] == '\0') {
        return {};
    }

    return fs::path(home) / ".config";
}
#endif

}  // namespace

fs::path defaultConfigPath() {
    const char* configPath = std::getenv("SGUMI_CONFIG_PATH");

    if (configPath && configPath[0] != '\0') {
        return fs::path(configPath);
    }

#if defined(_WIN32)
    // Roaming rather than Local, to match what the other two platforms do with
    // this file.
    const fs::path appData = knownFolderPath(FOLDERID_RoamingAppData);

    if (appData.empty()) {
        return fs::path(kConfigFileName);
    }

    return appData / "Eastern Edge" / kConfigFileName;
#elif defined(__APPLE__)
    // Application Support rather than anything XDG.
    const char* home = std::getenv("HOME");

    if (!home || home[0] == '\0') {
        return fs::path(kConfigFileName);
    }

    return fs::path(home) / "Library" / "Application Support" / "Eastern Edge" /
           kConfigFileName;
#else
    const fs::path configHome = xdgConfigHome();

    if (configHome.empty()) {
        return fs::path(kConfigFileName);
    }

    return configHome / "eastern-edge" / kConfigFileName;
#endif
}

const char* imguiIniPath() {
    static const std::string path = [] {
        fs::path p = defaultConfigPath();
        p.replace_filename(kImGuiFileName);

        // ImGui will not create the directory and will not say that it could
        // not. Normally the config save has already made it, but that runs
        // later than ImGui init, and a first launch that crashed before then
        // would leave nowhere to write.
        if (p.has_parent_path()) {
            std::error_code ec;
            fs::create_directories(p.parent_path(), ec);
        }

        return p.string();
    }();

    return path.c_str();
}

}  // namespace paths
