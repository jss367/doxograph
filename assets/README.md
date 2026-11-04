# Doxograph logo

Two broad interlocking coral strands form a sculptural D on an ink-blue
background. Their crossings connect the mark to Doxograph's linked claims
and sources, with open negative space that holds up at small sizes.

`icon.png` is the 1024 × 1024 master. `native/icon.png` is an identical copy
consumed by the Mac build. `doxograph/static/favicon.png` is a 256 × 256 copy
shared by the browser favicon and app header. Keep the square background in
these files; the header and Mac icon renderer apply their own rounded corners.

After replacing the master, regenerate the derived assets from the repo root:

```sh
cp assets/icon.png native/icon.png
sips -z 256 256 assets/icon.png --out doxograph/static/favicon.png
swift native/tools/make-icon.swift native/Doxograph.icns
```

Change the favicon version in `doxograph/static/index.html` when replacing it
so browsers fetch the new artwork. The Mac build regenerates its icon when
the source changes.

The artwork was created with the built-in image generation tool. The generation
prompt is recorded in [logo-prompt.txt](logo-prompt.txt).
