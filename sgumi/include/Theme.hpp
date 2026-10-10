#pragma once

// Visual theme for SGUMI.
//
// Copied verbatim from Software_2027's libs/eer_gfx at commit ff6a7f6, with
// two mechanical changes: the include path, and EER_GFX_THEME_EDITOR renamed
// to SGUMI_THEME_EDITOR. Kept otherwise byte-identical so that
//
//   git diff ff6a7f6..HEAD -- libs/eer_gfx/src/Theme.cpp
//
// over there still reads cleanly against this copy. Diverge freely once there
// is a reason to -- this is a fork point, not a subscription.
//
// Everything worth tweaking lives in the block at the top of src/Theme.cpp --
// colours, spacing, rounding and the UI font size are all in one place there.
// Call this once after ImGui::CreateContext(), instead of StyleColorsDark().
void applyTheme();

#if defined(SGUMI_THEME_EDITOR)

// Live colour editor, compiled in via -DSGUMI_THEME_EDITOR=ON (the default)
// and toggled at runtime with F10. Call once per frame between NewFrame() and
// Render().
//
// Style colours are read by ImGui every frame, so edits apply instantly -- no
// restart needed. Fonts are the exception: they bake into a texture atlas at
// load, so kFontSizePx still needs a rebuild.
//
// "Copy values to clipboard" emits the constexpr block to paste back into
// src/Theme.cpp, which is the only way to make changes persist.
void drawThemeEditor(bool* open);

#endif