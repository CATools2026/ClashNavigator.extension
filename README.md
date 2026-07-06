# Clash Navigator v2.0.0 (pyRevit extension)

Author: **Chulan Adasuriya**

Load one or many Navisworks clash reports (**XML or HTML**), switch between them
with the **Report** dropdown, browse each clash on a single row with **both**
clashing Element IDs (host + link) side by side, preview the clash viewpoint
image, then **select, zoom to, copy and colour-highlight** the elements in the
active Revit view. Elements are resolved in the host model *or* in any loaded
Revit link.

## Features

- **Load Report (XML / HTML)** — load a single Navisworks clash report. HTML
  (tabular) exports also load the clash viewpoint images.
- **Load Multiple...** — load several reports at once; switch between them with
  the Report dropdown. Files that fail to parse are skipped and reported.
- **Report dropdown** — pick the active report; the grid, levels, filters,
  statuses and image preview all switch to it. **Remove This Report** drops the
  active one.
- **Last Report / Reset Last Report** — reload the most recently used report, or
  forget it.
- One row per clash: **Host Model ID**, **Found (Host)**, **Link Model ID**,
  **Found (Link)**, combined item names, and an image indicator.
- **Select + Copy Host ID / Select + Copy Link ID** — select and zoom to the
  element *and* copy its Element ID to the clipboard.
- **Select Both + Zoom**, **Highlight (orange)**, **Clear Highlights**.
- **Clash image preview** panel with **Open Full Size**.
- **Level / Status / Discipline / In-Model** filters, search, editable status
  with per-report persistence, and **Export Status CSV**.

## Requirements

- pyRevit 5.x (IronPython 2.7 engine), Revit 2021-2026.
- For HTML: export the Navisworks report as **HTML (Tabular)** with *Element ID*
  included, and keep the images folder next to the `.html` file.
