#pragma once

#include <SDL3/SDL.h>
#include "imgui.h"

#include <cstddef>
#include <string>
#include <vector>

// Decoding a PNG into a GPU texture, for the label preview.
//
// Ported from Software_2027's libs/eer_gfx/src/Image.cpp, with one difference
// that matters: over there textures are loaded once at startup and never
// released, because they are logos. Here a new one arrives every time the
// preview is refreshed, so this owns its texture and frees the old one --
// otherwise every preview would leak a few megabytes of VRAM.

namespace image {

// Holds one texture and releases it on destruction or replacement. Not
// copyable, because two of these freeing the same texture would be a
// double-free.
class Texture {
public:
    Texture() = default;
    ~Texture();

    Texture(const Texture&) = delete;
    Texture& operator=(const Texture&) = delete;

    // Decodes a PNG and replaces whatever was held. Returns false and keeps
    // the previous texture if decoding or upload fails, so a bad response does
    // not blank a preview that was fine.
    bool load(SDL_GPUDevice* device, const void* data, size_t length);

    // Frees the texture. Safe to call twice, and called by the destructor --
    // but the caller must do it explicitly before the GPU device is destroyed,
    // since releasing a texture needs the device that made it.
    void reset();

    bool valid() const { return texture_ != nullptr; }
    int width() const { return width_; }
    int height() const { return height_; }

    // The ImGui SDLGPU3 backend expects ImTextureID to be the SDL_GPUTexture*
    // itself, so this can be handed straight to ImGui::Image.
    ImTextureID id() const { return (ImTextureID)(intptr_t)texture_; }

private:
    SDL_GPUDevice* device_ = nullptr;
    SDL_GPUTexture* texture_ = nullptr;
    int width_ = 0;
    int height_ = 0;
};

// ---------------------------------------------------------------------------
// Images on the CPU
//
// Only image printing needs these. A label rendered by lipgloss goes straight
// from bytes to texture and is never touched, but an image someone picked off
// their disk has to be measured, turned, fitted to the roll and re-encoded
// before it is worth sending -- and every one of those steps has to happen to
// the bytes, not to the copy on the GPU.
// ---------------------------------------------------------------------------

// A decoded image, RGBA, eight bits a channel.
struct Bitmap {
    std::vector<unsigned char> pixels;  // width * height * 4
    int width = 0;
    int height = 0;

    bool valid() const { return width > 0 && height > 0; }
};

// Anything stb_image reads: PNG, JPEG, BMP, GIF, TGA, PSD. The picker offers
// that same list, so what can be chosen is what can be decoded.
bool decode(const void* data, size_t length, Bitmap& out);

// Reads the file and decodes it. error is filled when it returns false, and is
// worth showing -- "no such file" and "not an image" look identical otherwise.
bool decodeFile(const char* path, Bitmap& out, std::string& error);

// Quarter turns clockwise, 0-3. Anything else is taken modulo 4.
Bitmap rotated(const Bitmap& source, int quarterTurns);

// Resampled to exactly these dimensions. Both must be at least 1.
Bitmap scaled(const Bitmap& source, int width, int height);

// Centred on an opaque white field of exactly these dimensions, for an image
// that is to keep its proportions rather than be distorted to fill a label.
//
// A source already at least this large in one direction is not cropped, it
// simply is not padded in that direction.
Bitmap paddedTo(const Bitmap& source, int width, int height);

// PNG bytes. lipgloss writes whatever arrives to a .png and hands it to the
// printer, so this is the last point at which the image is ours.
bool encodePng(const Bitmap& source, std::string& out);

}  // namespace image
