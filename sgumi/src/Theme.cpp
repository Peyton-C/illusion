#include "Theme.hpp"

#include "imgui.h"

#if defined(__APPLE__)
// For the system UI font, which is asked for rather than guessed at. CoreText
// is a C API, so this file stays plain C++ on Apple platforms.
#include <CoreText/CoreText.h>
#include <climits>
#endif

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <iostream>
#include <string>

// ---------------------------------------------------------------------------
// TWEAK ME
//
// Everything below this block is just plumbing that maps these values onto
// ImGui's ~55 individual style colours. Change numbers here, rebuild, look.
//
// Or use the live editor (drawThemeEditor) to fiddle at runtime and paste the
// results back into this block.
// ---------------------------------------------------------------------------
namespace theme {

// Colours are 0xRRGGBB. Alpha is separate where it matters.
//
// The teals are sampled from the app's icon background
constexpr unsigned kBgWindow    = 0x14161B; // outermost background
constexpr unsigned kBgPanel     = 0x1B1E25; // child windows, popups, menus
constexpr unsigned kBgFrame     = 0x232732; // inputs, buttons, sliders at rest
constexpr unsigned kBgFrameHot  = 0x24343B; // ...hovered, a teal-tinted lift
constexpr unsigned kBgFrameOn   = 0x015E69; // ...held or active

// Structural, not decorative. This one value drives ImGuiCol_Border,
// ImGuiCol_Separator AND the table grid (see TableBorderStrong below), so a
// saturated colour here paints every section rule and every row line in the
// accent and leaves nothing quiet for the accent to stand against.
constexpr unsigned kBorder      = 0x243038;

// Chrome: title bars, the menu bar and table header rows.
constexpr unsigned kChrome      = 0x0E3138; // at rest, and unfocused windows
constexpr unsigned kChromeOn    = 0x015E69; // the focused window's title bar

// The icon's sash, a warm off-white.
constexpr unsigned kText        = 0xEDEAE4;
constexpr unsigned kTextDim     = 0x98A1B0; // disabled text, hints

// One hue, three lightnesses: hover brightens, pressed deepens. Doing it this
// way means interaction reads as intensity rather than as a colour change,
// which is what keeps it legible next to the status colours below.
constexpr unsigned kAccent      = 0x018796; // icon ground; selection, checks
constexpr unsigned kAccentHot   = 0x2ABECF; // the icon's bright speckle
constexpr unsigned kAccentDim   = 0x015E69;

// Deliberately NOT drawn from the icon. Its lettering is a pink goldish copper 
// (#B76E4E deep, #DB8275 bright) which is pretty similar to kDanger
constexpr unsigned kWarning     = 0xE5A54B;
constexpr unsigned kDanger      = 0xE05C5C;

// Type. Set to 0 to keep ImGui's built-in ProggyClean and see the difference.
// Note: fonts bake into a texture atlas at load, so unlike colours this is
// NOT live-editable -- changing it needs a rebuild.
constexpr float kFontSizePx     = 16.0f;

// Spacing. ImGui's defaults are much tighter than a normal desktop app --
// most of the "designed" feeling comes from these three, not the colours.
constexpr ImVec2 kWindowPadding = ImVec2(16.0f, 14.0f);
constexpr ImVec2 kFramePadding  = ImVec2(12.0f,  7.0f);
constexpr ImVec2 kItemSpacing   = ImVec2(10.0f,  8.0f);
constexpr ImVec2 kInnerSpacing  = ImVec2( 8.0f,  6.0f);

// Rounding. 0 everywhere is the stock ImGui look.
constexpr float kRoundFrame     = 6.0f;
constexpr float kRoundWindow    = 8.0f;
constexpr float kRoundChild     = 8.0f;
constexpr float kRoundPopup     = 8.0f;
constexpr float kRoundGrab      = 6.0f;
constexpr float kRoundScrollbar = 8.0f;
constexpr float kRoundTab       = 6.0f;

// Borders. ImGui draws 1px lines around nearly everything by default;
// modern UI separates with background tone instead. 0 disables.
constexpr float kBorderWindow   = 0.0f;
constexpr float kBorderFrame    = 0.0f;
constexpr float kBorderChild    = 1.0f;
constexpr float kBorderPopup    = 1.0f;

constexpr float kScrollbarSize  = 12.0f;
constexpr float kGrabMinSize    = 12.0f;

} // namespace theme
// ---------------------------------------------------------------------------
// End of tweakables.
// ---------------------------------------------------------------------------

namespace {

ImVec4 rgb(unsigned hex, float alpha = 1.0f) {
    return ImVec4(
        static_cast<float>((hex >> 16) & 0xFF) / 255.0f,
        static_cast<float>((hex >>  8) & 0xFF) / 255.0f,
        static_cast<float>((hex      ) & 0xFF) / 255.0f,
        alpha);
}

ImVec4 withAlpha(ImVec4 colour, float alpha) {
    colour.w = alpha;
    return colour;
}

#if defined(SGUMI_THEME_EDITOR)
unsigned toHex(const ImVec4& colour) {
    auto channel = [](float v) {
        return static_cast<unsigned>(std::clamp(v, 0.0f, 1.0f) * 255.0f + 0.5f);
    };

    return (channel(colour.x) << 16) | (channel(colour.y) << 8) | channel(colour.z);
}
#endif

// Runtime mirror of the palette above. ImGui reads style.Colors[] every frame,
// so mutating this and re-applying takes effect on the next frame -- that is
// what makes the live editor possible.
struct LiveColours {
    ImVec4 bgWindow;
    ImVec4 bgPanel;
    ImVec4 bgFrame;
    ImVec4 bgFrameHot;
    ImVec4 bgFrameOn;
    ImVec4 border;
    ImVec4 chrome;
    ImVec4 chromeOn;
    ImVec4 text;
    ImVec4 textDim;
    ImVec4 accent;
    ImVec4 accentHot;
    ImVec4 accentDim;
    ImVec4 warning;
    ImVec4 danger;
};

LiveColours g_colours;

void seedFromConstants() {
    using namespace theme;

    g_colours.bgWindow   = rgb(kBgWindow);
    g_colours.bgPanel    = rgb(kBgPanel);
    g_colours.bgFrame    = rgb(kBgFrame);
    g_colours.bgFrameHot = rgb(kBgFrameHot);
    g_colours.bgFrameOn  = rgb(kBgFrameOn);
    g_colours.border     = rgb(kBorder);
    g_colours.chrome     = rgb(kChrome);
    g_colours.chromeOn   = rgb(kChromeOn);
    g_colours.text       = rgb(kText);
    g_colours.textDim    = rgb(kTextDim);
    g_colours.accent     = rgb(kAccent);
    g_colours.accentHot  = rgb(kAccentHot);
    g_colours.accentDim  = rgb(kAccentDim);
    g_colours.warning    = rgb(kWarning);
    g_colours.danger     = rgb(kDanger);
}

void applyColours(const LiveColours& t) {
    ImVec4* c = ImGui::GetStyle().Colors;

    c[ImGuiCol_Text]                  = t.text;
    c[ImGuiCol_TextDisabled]          = t.textDim;

    c[ImGuiCol_WindowBg]              = t.bgWindow;
    c[ImGuiCol_ChildBg]               = t.bgPanel;
    c[ImGuiCol_PopupBg]               = t.bgPanel;
    c[ImGuiCol_MenuBarBg]             = t.chrome;

    c[ImGuiCol_Border]                = t.border;
    c[ImGuiCol_BorderShadow]          = rgb(0x000000, 0.0f);

    c[ImGuiCol_FrameBg]               = t.bgFrame;
    c[ImGuiCol_FrameBgHovered]        = t.bgFrameHot;
    c[ImGuiCol_FrameBgActive]         = t.bgFrameOn;

    // Active is the brighter of the two, so a focused window's title bar is
    // visibly ahead of an unfocused one. These were both bgPanel before, which
    // meant a stack of windows gave no clue which one had the keyboard.
    c[ImGuiCol_TitleBg]               = t.chrome;
    c[ImGuiCol_TitleBgActive]         = t.chromeOn;
    c[ImGuiCol_TitleBgCollapsed]      = withAlpha(t.chrome, 0.75f);

    c[ImGuiCol_ScrollbarBg]           = withAlpha(t.bgWindow, 0.0f);
    c[ImGuiCol_ScrollbarGrab]         = t.bgFrameHot;
    c[ImGuiCol_ScrollbarGrabHovered]  = t.bgFrameOn;
    c[ImGuiCol_ScrollbarGrabActive]   = t.accentDim;

    c[ImGuiCol_CheckMark]             = t.accent;
    c[ImGuiCol_SliderGrab]            = t.accentDim;
    c[ImGuiCol_SliderGrabActive]      = t.accent;

    c[ImGuiCol_Button]                = t.bgFrame;
    c[ImGuiCol_ButtonHovered]         = t.bgFrameHot;
    c[ImGuiCol_ButtonActive]          = t.bgFrameOn;

    c[ImGuiCol_Header]                = withAlpha(t.accentDim, 0.35f);
    c[ImGuiCol_HeaderHovered]         = withAlpha(t.accentDim, 0.55f);
    c[ImGuiCol_HeaderActive]          = withAlpha(t.accentDim, 0.75f);

    c[ImGuiCol_Separator]             = t.border;
    c[ImGuiCol_SeparatorHovered]      = t.accentDim;
    c[ImGuiCol_SeparatorActive]       = t.accent;

    c[ImGuiCol_ResizeGrip]            = withAlpha(t.bgFrameHot, 0.6f);
    c[ImGuiCol_ResizeGripHovered]     = t.accentDim;
    c[ImGuiCol_ResizeGripActive]      = t.accent;

    c[ImGuiCol_Tab]                   = t.bgPanel;
    c[ImGuiCol_TabHovered]            = t.bgFrameHot;
    c[ImGuiCol_TabSelected]           = t.bgFrameOn;
    c[ImGuiCol_TabSelectedOverline]   = t.accent;
    c[ImGuiCol_TabDimmed]             = t.bgPanel;
    c[ImGuiCol_TabDimmedSelected]     = t.bgFrame;

    c[ImGuiCol_PlotLines]             = t.accent;
    c[ImGuiCol_PlotLinesHovered]      = t.accentHot;
    c[ImGuiCol_PlotHistogram]         = t.warning;
    c[ImGuiCol_PlotHistogramHovered]  = t.danger;

    c[ImGuiCol_TableHeaderBg]         = t.chrome;
    c[ImGuiCol_TableBorderStrong]     = t.border;
    c[ImGuiCol_TableBorderLight]      = withAlpha(t.border, 0.5f);
    c[ImGuiCol_TableRowBg]            = rgb(0x000000, 0.0f);
    c[ImGuiCol_TableRowBgAlt]         = rgb(0xFFFFFF, 0.02f);

    c[ImGuiCol_TextSelectedBg]        = withAlpha(t.accent, 0.35f);
    c[ImGuiCol_DragDropTarget]        = t.accent;
    c[ImGuiCol_NavCursor]             = t.accent;
    c[ImGuiCol_NavWindowingHighlight] = withAlpha(t.accent, 0.7f);
    c[ImGuiCol_NavWindowingDimBg]     = rgb(0x000000, 0.5f);
    c[ImGuiCol_ModalWindowDimBg]      = rgb(0x000000, 0.6f);
}

void applyMetrics() {
    using namespace theme;

    ImGuiStyle& s = ImGui::GetStyle();

    s.WindowPadding     = kWindowPadding;
    s.FramePadding      = kFramePadding;
    s.ItemSpacing       = kItemSpacing;
    s.ItemInnerSpacing  = kInnerSpacing;
    s.CellPadding       = kInnerSpacing;

    s.WindowRounding    = kRoundWindow;
    s.ChildRounding     = kRoundChild;
    s.FrameRounding     = kRoundFrame;
    s.PopupRounding     = kRoundPopup;
    s.GrabRounding      = kRoundGrab;
    s.ScrollbarRounding = kRoundScrollbar;
    s.TabRounding       = kRoundTab;

    s.WindowBorderSize  = kBorderWindow;
    s.FrameBorderSize   = kBorderFrame;
    s.ChildBorderSize   = kBorderChild;
    s.PopupBorderSize   = kBorderPopup;

    s.ScrollbarSize     = kScrollbarSize;
    s.GrabMinSize       = kGrabMinSize;

    s.WindowTitleAlign  = ImVec2(0.0f, 0.5f);
}

// Load a reasonable UI font from the system. ImGui's built-in ProggyClean is
// a 13px bitmap-style face designed for debug overlays, and it is by far the
// strongest visual signal that something is "an ImGui app". Falls back to the
// default silently if nothing here is present.
#if defined(__APPLE__)

// Where the OS says its own UI font lives.
//
// Apple platforms get this instead of a candidate list because a list cannot be
// written for them: macOS keeps the system font at /System/Library/Fonts/SFNS.ttf,
// iOS keeps it somewhere else that is neither documented nor stable across
// releases, and the whole point of the font is to be the one the OS is already
// using. CoreText knows, so it is asked.
//
// A pure C API despite the Apple branding -- no Objective-C, so this stays an
// ordinary .cpp and iOS does not need the OBJCXX language enabled.
//
// Empty on failure, which lands on the same fallback as a missing file.
std::string systemUiFontPath() {
    CTFontRef font =
        CTFontCreateUIFontForLanguage(kCTFontUIFontSystem, 0.0, nullptr);

    if (!font) {
        return {};
    }

    CFURLRef url = static_cast<CFURLRef>(
        CTFontCopyAttribute(font, kCTFontURLAttribute));
    CFRelease(font);

    if (!url) {
        return {};
    }

    char path[PATH_MAX] = {};
    const bool ok = CFURLGetFileSystemRepresentation(
        url, true, reinterpret_cast<UInt8*>(path), sizeof path);
    CFRelease(url);

    return ok ? std::string(path) : std::string();
}

#endif  // __APPLE__

void loadUiFont() {
    if (theme::kFontSizePx <= 0.0f) {
        return;
    }

#if defined(__APPLE__)
    // Tried before the list below, which on Apple holds only the fonts worth
    // falling back to if CoreText somehow declines to answer.
    if (const std::string systemFont = systemUiFontPath(); !systemFont.empty()) {
        if (ImGui::GetIO().Fonts->AddFontFromFileTTF(systemFont.c_str(),
                                                     theme::kFontSizePx)) {
            return;
        }

        // Reached if the file is there but unreadable or unparseable -- a font
        // collection needing an explicit face index would look like this. Worth
        // naming, because the fallback below hides it otherwise.
        std::cerr << "Theme: could not load the system font at " << systemFont
                  << ", falling back" << std::endl;
    }
#endif

    const char* candidates[] = {
#if defined(__APPLE__)
        // Only reachable if CoreText failed. iOS has none of these, and does
        // not need them: it either gets the system font above or the ImGui
        // default.
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/SFNSDisplay.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Comic Sans MS Bold.ttf",
        "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf",
#elif defined(_WIN32)
        // Semibold ahead of Regular on purpose. Windows desktops are usually
        // at a fractional scale (125% is the common one), which lands glyphs
        // between physical pixels, and stb_truetype does not hint, so nothing
        // snaps back to the grid. Segoe UI Regular's stems are thin enough
        // that the resulting half-coverage reads as grey rather than black.
        // Semibold has the ink to survive it. See RasterizerMultiply below.
        "C:\\Windows\\Fonts\\seguisb.ttf",
        "C:\\Windows\\Fonts\\segoeui.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
#else
        "/usr/share/fonts/adwaita-mono-fonts/AdwaitaMono-Regular.ttf",
        "/usr/share/fonts/open-sans/OpenSans-Regular.ttf",
        "/usr/share/fonts/google-droid-sans-fonts/DroidSans.ttf",
        "/usr/share/fonts/google-noto/NotoSansMath-Regular.ttf",
        "/usr/share/fonts/google-noto-vf/NotoSans[wght].ttf",
        "/usr/share/fonts/truetype/inter/Inter-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/roboto/Roboto-Medium.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
#endif
    };

    ImFontConfig cfg;

#if defined(_WIN32)
    // Brightens the rasterized coverage. Same problem as the face choice above
    // -- unhinted glyphs at a fractional desktop scale never fully cover a
    // pixel, so the text comes out washed out -- and ImGui documents this field
    // as the workaround for exactly that. Cosmetic only: it does not move any
    // glyph back onto the pixel grid, it just stops the smear from reading as
    // grey. The real fix is to lay out in device pixels.
    cfg.RasterizerMultiply = 1.3f;
#endif

    for (const char* path : candidates) {
        if (!std::filesystem::exists(path)) {
            continue;
        }

        if (ImGui::GetIO().Fonts->AddFontFromFileTTF(
                path, theme::kFontSizePx, &cfg)) {
            return;
        }
    }

    std::cerr << "Theme: no system UI font found, using ImGui default"
              << std::endl;
}

#if defined(SGUMI_THEME_EDITOR)

// One row of the live editor.
void colourRow(const char* label, const char* comment, ImVec4& target) {
    ImGui::ColorEdit3(
        label,
        &target.x,
        ImGuiColorEditFlags_DisplayHex | ImGuiColorEditFlags_NoAlpha);

    if (comment && ImGui::IsItemHovered()) {
        ImGui::SetTooltip("%s", comment);
    }
}

std::string exportConstants() {
    struct Row { const char* name; const ImVec4* colour; };

    const Row rows[] = {
        { "kBgWindow   ", &g_colours.bgWindow   },
        { "kBgPanel    ", &g_colours.bgPanel    },
        { "kBgFrame    ", &g_colours.bgFrame    },
        { "kBgFrameHot ", &g_colours.bgFrameHot },
        { "kBgFrameOn  ", &g_colours.bgFrameOn  },
        { "kBorder     ", &g_colours.border     },
        { "kChrome     ", &g_colours.chrome     },
        { "kChromeOn   ", &g_colours.chromeOn   },
        { "kText       ", &g_colours.text       },
        { "kTextDim    ", &g_colours.textDim    },
        { "kAccent     ", &g_colours.accent     },
        { "kAccentHot  ", &g_colours.accentHot  },
        { "kAccentDim  ", &g_colours.accentDim  },
        { "kWarning    ", &g_colours.warning    },
        { "kDanger     ", &g_colours.danger     },
    };

    std::string out;
    char line[128];

    for (const Row& row : rows) {
        std::snprintf(
            line,
            sizeof(line),
            "constexpr unsigned %s = 0x%06X;\n",
            row.name,
            toHex(*row.colour));

        out += line;
    }

    return out;
}

#endif // SGUMI_THEME_EDITOR

} // namespace

void applyTheme() {
    loadUiFont();
    seedFromConstants();
    applyMetrics();
    applyColours(g_colours);
}

#if defined(SGUMI_THEME_EDITOR)

void drawThemeEditor(bool* open) {
    if (open && !*open) {
        return;
    }

    ImGui::SetNextWindowSize(ImVec2(340.0f, 0.0f), ImGuiCond_FirstUseEver);

    if (!ImGui::Begin("Theme", open)) {
        ImGui::End();
        return;
    }

    ImGui::TextDisabled("Edits apply instantly. Paste back to persist.");
    ImGui::Spacing();

    colourRow("Window bg",   "Outermost background",            g_colours.bgWindow);
    colourRow("Panel bg",    "Child windows, popups, menus",    g_colours.bgPanel);
    colourRow("Frame",       "Inputs and buttons at rest",      g_colours.bgFrame);
    colourRow("Frame hover", "Inputs and buttons hovered",      g_colours.bgFrameHot);
    colourRow("Frame active","Inputs and buttons held",         g_colours.bgFrameOn);
    colourRow("Border",      "Separators, child window edges",  g_colours.border);

    ImGui::Spacing();

    colourRow("Chrome",      "Menu bar, table headers, title bars", g_colours.chrome);
    colourRow("Chrome focus","Focused window's title bar",      g_colours.chromeOn);

    ImGui::Spacing();

    colourRow("Text",        "Primary text",                    g_colours.text);
    colourRow("Text dim",    "Disabled text and hints",         g_colours.textDim);

    ImGui::Spacing();

    colourRow("Accent",      "Checks, selection, active tab",   g_colours.accent);
    colourRow("Accent hot",  "Accent, hovered",                 g_colours.accentHot);
    colourRow("Accent dim",  "Accent, recessed (headers)",      g_colours.accentDim);

    ImGui::Spacing();

    colourRow("Warning",     "Currently only plot histograms",  g_colours.warning);
    colourRow("Danger",      "Currently only plot histograms",  g_colours.danger);

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();

    if (ImGui::Button("Copy values to clipboard")) {
        ImGui::SetClipboardText(exportConstants().c_str());
    }

    ImGui::SameLine();

    if (ImGui::Button("Reset")) {
        seedFromConstants();
    }

    ImGui::Spacing();
    ImGui::TextDisabled("Paste into the TWEAK ME block in src/Theme.cpp.");

    // Cheap enough to just re-apply every frame rather than track dirtiness.
    applyColours(g_colours);

    ImGui::End();
}

#endif // SGUMI_THEME_EDITOR