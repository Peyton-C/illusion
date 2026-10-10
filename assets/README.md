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

## Apple Icon Composer
Both the generic and Apple formatted icons are exported from Apple Icon composer, the SGUMI icon is exported with liquid glass enabled.