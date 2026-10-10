# assets
Images used in illusion.

You can select a different icon for SGUMI at build time with: `-DSGUMI_ICON_NAME=<name>`

## Extensions
| extension | type                        |
|-----------|-----------------------------|
| .ico      | Windows icon format         |
| .icon     | Modern Apple icon format    |

## Convert to ico
Using imagemagick:
```sh
magick IN.png -define icon:auto-resize=256,128,64,48,32,16 OUT.ico
```

## Resize for Linux
The Linux install wants `<name>-48.png`, `-128.png`, `-256.png` and `-512.png` beside the 1024x1024 `<name>.png`. The 128 is also compiled into SGUMI as its window icon:
```sh
for s in 48 128 256 512; do magick IN.png -resize ${s}x${s} -depth 8 OUT-$s.png; done
```

## Apple Icon Composer
Both the generic and Apple formatted icons are exported from Apple Icon composer, the SGUMI icon is exported with liquid glass enabled.