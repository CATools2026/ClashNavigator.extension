# -*- coding: utf-8 -*-
"""Clash Navigator

Load a Navisworks clash report (XML or HTML), browse the clashes grouped one
clash per row with BOTH element IDs side by side, preview the clash viewpoint
image (from HTML reports or image-enabled XML reports), then click to select,
zoom to and (optionally) colour-highlight the elements in the active view.

Modeless window + ExternalEvent for safe Revit API access.
"""

__title__ = "Clash\nNavigator"
__author__ = "Chulan Adasuriya"
__version__ = "2.0.0"
__doc__ = ("Load one or many Navisworks clash reports (.xml or .html), switch "
           "between them, filter clashes by level, preview the clash image, and "
           "click to select / zoom / copy / highlight both clashing elements.")

import os
import io
import re
import json
import xml.etree.ElementTree as ET

# Keep the pyRevit engine alive after the command finishes. REQUIRED for a
# modeless window: without it pyRevit tears the engine down and any later
# interaction with the window or its ExternalEvent hard-crashes Revit.
__persistentengine__ = True

import clr
clr.AddReference("System.Data")
from System.Data import DataTable
from System.Collections.Generic import List

from pyrevit import forms, revit, DB, script
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent
from System import Uri, UriKind
from System.Windows import Thickness, Visibility, Clipboard
from System.Windows.Controls import CheckBox
from System.Windows.Media import SolidColorBrush, Color
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption

logger = script.get_logger()
cfg = script.get_config()

# light text for checkboxes created at runtime inside the dark popups
LIGHT_FG = SolidColorBrush(Color.FromRgb(236, 232, 247))

STATUS_OPTIONS = ["new", "active", "pending", "reviewed", "approved", "resolved"]

IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".gif")


def rdoc():
    """Live current Document (never a stale cached reference)."""
    return revit.doc


def ruidoc():
    """Live current UIDocument."""
    return revit.uidoc


# --------------------------------------------------------------------------- #
# External event: lets the modeless window run Revit API calls (selection,
# zoom, transactions) inside a valid API context.
# --------------------------------------------------------------------------- #
class _ApiHandler(IExternalEventHandler):
    def __init__(self):
        self.queue = []

    def Execute(self, uiapp):
        actions = self.queue
        self.queue = []
        for fn in actions:
            try:
                fn()
            except Exception as ex:
                logger.debug("api action: %s", ex)

    def GetName(self):
        return "Clash Navigator External Event"


def write_text(path, text):
    """Write UTF-8 text on both IronPython 2.7 and CPython 3."""
    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8")
    except Exception:
        pass
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read_text(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_text_any(path):
    """Read text tolerating the encodings Navisworks HTML exports use."""
    for enc in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            with io.open(path, "r", encoding=enc) as f:
                return f.read()
        except Exception:
            continue
    with io.open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def make_eid(val):
    try:
        return DB.ElementId(int(val))
    except Exception:
        try:
            from System import Int64
            return DB.ElementId(Int64(int(val)))
        except Exception:
            return DB.ElementId.InvalidElementId


def get_solid_fill_id(document):
    try:
        for fp in DB.FilteredElementCollector(document).OfClass(DB.FillPatternElement):
            try:
                if fp.GetFillPattern().IsSolidFill:
                    return fp.Id
            except Exception:
                continue
    except Exception:
        pass
    return DB.ElementId.InvalidElementId


def discipline_from_nodes(nodes):
    joined = u" ".join(nodes).upper()
    if "ELVS" in joined or "-EL-" in joined:
        return "Security/EL"
    if "FFGS" in joined or "FIRE" in joined or "-ME-" in joined:
        return "Fire Fight"
    for n in reversed(nodes):
        low = n.lower()
        if low.endswith(".rvt") or ".rvt " in low or low.endswith(".nwc"):
            return n.split(".")[0][-14:]
    return "?"


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_tags(fragment):
    txt = _TAG_RE.sub(" ", fragment or u"")
    for a, b in ((u"&nbsp;", u" "), (u"&amp;", u"&"), (u"&lt;", u"<"),
                 (u"&gt;", u">"), (u"&quot;", u'"'), (u"&#39;", u"'")):
        txt = txt.replace(a, b)
    return _WS_RE.sub(u" ", txt).strip()


def url_to_path(src, base_dir):
    """Turn an img/anchor src from the HTML report into an absolute file path."""
    if not src:
        return None
    src = src.strip().replace("%20", " ")
    if src.lower().startswith("file:///"):
        src = src[8:]
    src = src.replace("/", os.sep)
    if not os.path.isabs(src):
        src = os.path.join(base_dir, src)
    return src if os.path.isfile(src) else None


# --------------------------------------------------------------------------- #
# Tickable dropdown filter (ToggleButton + Popup, managed in Python)
# --------------------------------------------------------------------------- #
class MultiFilter(object):
    def __init__(self, window, toggle, popup_ctrl, container, label):
        self.window    = window
        self.toggle    = toggle
        self.popup     = popup_ctrl
        self.container = container
        self.label     = label
        self.checks    = []
        self._suppress = False

        self.toggle.Click += self._on_toggle
        self.popup.Closed += self._on_closed

    def _on_toggle(self, s, e):
        self.popup.IsOpen = not self.popup.IsOpen
        self.toggle.IsChecked = self.popup.IsOpen

    def _on_closed(self, s, e):
        try:
            self.toggle.IsChecked = False
        except Exception:
            pass

    def populate(self, values):
        self._suppress = True
        try:
            self.container.Children.Clear()
            self.checks = []
            for v in values:
                cb = CheckBox()
                cb.Content   = v
                cb.IsChecked = True
                cb.Foreground = LIGHT_FG
                cb.Margin    = Thickness(3, 2, 3, 2)
                cb.Checked   += self._changed
                cb.Unchecked += self._changed
                self.container.Children.Add(cb)
                self.checks.append(cb)
        finally:
            self._suppress = False
        self._update_label()

    def selected(self):
        sel = set(str(c.Content) for c in self.checks if c.IsChecked)
        return None if len(sel) == len(self.checks) else sel

    def reset(self):
        self._suppress = True
        try:
            for c in self.checks:
                c.IsChecked = True
        finally:
            self._suppress = False
        self._update_label()

    def _changed(self, s, e):
        if self._suppress:
            return
        self._update_label()
        self.window._apply_if_ready()

    def _update_label(self):
        total = len(self.checks)
        sel   = sum(1 for c in self.checks if c.IsChecked)
        txt   = "All" if (total == 0 or sel == total) else ("None" if sel == 0 else "{} sel".format(sel))
        self.toggle.Content = "{}: {}".format(self.label, txt)


# --------------------------------------------------------------------------- #
# Main window — modeless
# --------------------------------------------------------------------------- #
class ClashWindow(forms.WPFWindow):

    def __init__(self):
        xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")
        forms.WPFWindow.__init__(self, xaml_path)

        self.all_rows     = []          # one dict per CLASH (both elements)
        self.visible_rows = []
        self.table        = None
        self._loading     = False
        self._selecting   = False
        self._resolve_cache = {}
        self.overridden   = []          # [(view_id, eid), ...]
        self.current_report = None
        self.status_overrides = {}
        self.links        = []
        self.reports      = []          # [{path, name, rows}, ...]
        self.current_index = -1
        self._switching   = False

        # external event for safe Revit API calls from this modeless window
        self.handler = _ApiHandler()
        self.ext_event = ExternalEvent.Create(self.handler)

        # branding: load the CA Tools logo and stamp the version footer
        self._load_logo()
        try:
            self.version_tb.Text = "v" + __version__
        except Exception as ex:
            logger.debug("version label: %s", ex)

        self._collect_links()

        # make status list for combo column
        from System.Collections.Generic import List as GList
        self.status_options = GList[str]()
        for s in STATUS_OPTIONS:
            self.status_options.Add(s)
        try:
            self.status_col.ItemsSource = self.status_options
        except Exception as ex:
            logger.debug("status combo bind: %s", ex)

        # wire buttons
        self.load_btn.Click          += self.on_load
        self.load_many_btn.Click     += self.on_load_many
        self.last_btn.Click          += self.on_load_last
        self.reset_last_btn.Click    += self.on_reset_last
        self.report_cb.SelectionChanged += self.on_report_changed
        self.close_report_btn.Click  += self.on_close_report
        self.level_cb.SelectionChanged += self.on_filter_changed
        self.search_tb.TextChanged   += self.on_filter_changed
        self.reset_filters_btn.Click += self.on_reset_filters
        self.grid.SelectionChanged   += self.on_row_changed
        self.grid.CellEditEnding     += self.on_cell_edit
        self.select_btn.Click        += self.on_select_1
        self.select2_btn.Click       += self.on_select_2
        self.both_btn.Click          += self.on_select_both
        self.highlight_btn.Click     += self.on_highlight
        self.clear_btn.Click         += self.on_clear
        self.export_btn.Click        += self.on_export_status
        self.open_img_btn.Click      += self.on_open_image

        # filters — pass popup control explicitly so Python owns open/close
        self.f_status  = MultiFilter(self, self.status_toggle,  self.status_popup,  self.status_list,  "Status")
        self.f_disc    = MultiFilter(self, self.disc_toggle,    self.disc_popup,    self.disc_list,    "Discipline")
        self.f_inmodel = MultiFilter(self, self.inmodel_toggle, self.inmodel_popup, self.inmodel_list, "In Model")

        # restore last report
        last = self._stored_path()
        self.last_btn.IsEnabled = bool(last)
        self.reset_last_btn.IsEnabled = bool(last)
        if last and os.path.isfile(last):
            if self._load_report(last, remember=False):
                self.set_status("Reloaded last report: {} ({} clashes). Load a new one or pick a level."
                                .format(os.path.basename(last), len(self.all_rows)))

    # ------------------------------------------------------------------ #
    # Branding
    # ------------------------------------------------------------------ #
    def _load_logo(self):
        """Load the bundled CA Tools logo into the header badge.

        Fails silently (leaves the badge empty) if the asset is missing, so a
        packaging slip can never crash the window.
        """
        try:
            path = os.path.join(os.path.dirname(__file__), "logo.png")
            if os.path.isfile(path):
                bi = BitmapImage()
                bi.BeginInit()
                bi.UriSource = Uri(path, UriKind.Absolute)
                bi.CacheOption = BitmapCacheOption.OnLoad
                bi.EndInit()
                self.logo_img.Source = bi
        except Exception as ex:
            logger.debug("logo load: %s", ex)

    # ------------------------------------------------------------------ #
    # Revit API calls
    # ------------------------------------------------------------------ #
    def run_in_revit(self, fn):
        """Queue a no-arg callable to run in a valid Revit API context."""
        self.handler.queue.append(fn)
        try:
            self.ext_event.Raise()
        except Exception as ex:
            logger.debug("raise: %s", ex)

    def _do_select(self, host_ids, link_results, zoom, link_for_zoom):
        """Runs inside the Revit API context (via external event)."""
        uidoc = ruidoc()
        doc = rdoc()
        if uidoc is None or doc is None:
            return
        refs = List[DB.Reference]()
        for res in link_results:
            try:
                refs.Add(DB.Reference(res["el"]).CreateLinkReference(res["link"]))
            except Exception as ex:
                logger.debug("link ref: %s", ex)

        if refs.Count == 0 and host_ids:
            idlist = List[DB.ElementId](host_ids)
            try:
                uidoc.Selection.SetElementIds(idlist)
            except Exception as ex:
                logger.debug("SetElementIds: %s", ex)
            if zoom:
                try:
                    uidoc.ShowElements(idlist)
                except Exception:
                    pass
        elif refs.Count > 0:
            for eid in host_ids:
                el = doc.GetElement(eid)
                if el:
                    try:
                        refs.Add(DB.Reference(el))
                    except Exception:
                        pass
            try:
                uidoc.Selection.SetReferences(refs)
            except Exception as ex:
                logger.debug("SetReferences: %s", ex)
            if zoom:
                if host_ids:
                    idlist = List[DB.ElementId](host_ids)
                    try:
                        uidoc.ShowElements(idlist)
                    except Exception:
                        pass
                elif link_for_zoom:
                    self._zoom_link(link_for_zoom)

    def _zoom_link(self, res):
        try:
            uidoc = ruidoc()
            if uidoc is None:
                return
            bb = res["el"].get_BoundingBox(None)
            if bb is None:
                return
            t  = res["link"].GetTotalTransform()
            p0 = t.OfPoint(bb.Min)
            p1 = t.OfPoint(bb.Max)
            aid = uidoc.ActiveView.Id
            for uv in uidoc.GetOpenUIViews():
                if uv.ViewId == aid:
                    try:
                        uv.ZoomAndCenterRectangle(p0, p1)
                    except Exception:
                        try:
                            uv.ZoomToFit()
                        except Exception:
                            pass
                    break
        except Exception as ex:
            logger.debug("zoom link: %s", ex)

    # ------------------------------------------------------------------ #
    # Data helpers
    # ------------------------------------------------------------------ #
    def _collect_links(self):
        d = rdoc()
        if d is None:
            return
        try:
            for li in DB.FilteredElementCollector(d).OfClass(DB.RevitLinkInstance):
                ld = li.GetLinkDocument()
                if ld:
                    self.links.append((li, ld))
        except Exception as ex:
            logger.debug("links: %s", ex)

    def resolve(self, eid_str):
        if not eid_str:
            return {"where": None, "eid": DB.ElementId.InvalidElementId}
        if eid_str in self._resolve_cache:
            return self._resolve_cache[eid_str]
        eid = make_eid(eid_str)
        res = {"where": None, "eid": eid}
        d = rdoc()
        try:
            el = d.GetElement(eid) if d is not None else None
            if el:
                res = {"where": "host", "eid": eid, "el": el}
        except Exception:
            pass
        if res["where"] is None:
            for li, ld in self.links:
                try:
                    le = ld.GetElement(eid)
                    if le:
                        res = {"where": "link", "eid": eid, "el": le, "link": li, "ldoc": ld}
                        break
                except Exception:
                    pass
        self._resolve_cache[eid_str] = res
        return res

    # ------------------------------------------------------------------ #
    # Parsing — XML
    # ------------------------------------------------------------------ #
    def parse_xml(self, path):
        """One row per clash. Both element IDs paired side by side."""
        rows = []
        base = os.path.dirname(path)
        root = ET.parse(path).getroot()
        for cr in root.findall(".//clashresult"):
            cname   = cr.get("name")   or ""
            cguid   = cr.get("guid")   or cname
            cstatus = cr.get("status") or ""
            dist    = cr.get("distance") or ""
            grid    = cr.findtext("gridlocation") or ""
            glevel  = grid.split(":")[-1].strip() if ":" in grid else ""

            # image (XML reports written with viewpoint images carry href)
            img = url_to_path(cr.get("href"), base)

            objs = []
            for ob in cr.findall(".//clashobject"):
                eid_val = None
                for oa in ob.findall("objectattribute"):
                    if (oa.findtext("name") or "") == "Element ID":
                        eid_val = oa.findtext("value")
                if eid_val is None:
                    continue
                layer = ob.findtext("layer") or glevel or "(none)"
                nodes = [(n.text or "") for n in ob.findall("./pathlink/node")]
                iname = ""
                for st in ob.findall("./smarttags/smarttag"):
                    if (st.findtext("name") or "") == "Item Name":
                        iname = st.findtext("value") or ""
                objs.append({"id": eid_val, "item": iname, "level": layer,
                             "disc": discipline_from_nodes(nodes)})
            if not objs:
                continue
            e1 = objs[0]
            e2 = objs[1] if len(objs) > 1 else None
            rows.append(self._make_row(cname, cguid, cstatus, dist,
                                       e1.get("level") or glevel or "(none)",
                                       e1, e2, img))
        return rows

    # ------------------------------------------------------------------ #
    # Parsing — HTML (Navisworks tabular HTML export, images included)
    # ------------------------------------------------------------------ #
    def parse_html(self, path):
        html = read_text_any(path)
        base = os.path.dirname(path)
        rows = []

        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
        for tr in trs:
            # both element IDs in this row (Item 1 / Item 2 columns)
            ids = re.findall(r"Element\s*ID[^0-9]{0,10}(\d{4,})", tr, re.I)
            if not ids:
                continue
            ids = ids[:2]

            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
            texts = [strip_tags(c) for c in cells]
            rowtxt = strip_tags(tr)

            # clash name
            m = re.search(r"\bClash\s*[0-9]+\b", rowtxt)
            cname = m.group(0).replace(" ", "") if m else ""
            if not cname:
                for t in texts:
                    if t and "element id" not in t.lower() and len(t) < 40 \
                            and not re.match(r"^[\d.\-]+$", t):
                        cname = t
                        break
            if not cname:
                cname = "Clash{}".format(len(rows) + 1)

            # status
            cstatus = ""
            low_row = rowtxt.lower()
            for st in STATUS_OPTIONS:
                if re.search(r"\b" + st + r"\b", low_row):
                    cstatus = st
                    break

            # level from grid location "A-3 : L35_FFL"
            level = "(none)"
            m = re.search(r"[A-Za-z0-9_.\-]+\s*:\s*([A-Za-z0-9_.\-]+)", rowtxt)
            if m:
                level = m.group(1)

            # distance
            dist = ""
            m = re.search(r"(-?\d+\.\d+)\s*m?\b", rowtxt)
            if m:
                dist = m.group(1)

            # item names: first short, non-numeric text after each Element ID cell
            items = self._html_item_names(texts, ids)

            # clash viewpoint image
            img = None
            m = re.search(r'<img[^>]+src\s*=\s*["\']([^"\']+)["\']', tr, re.I)
            if not m:
                m = re.search(r'<a[^>]+href\s*=\s*["\']([^"\']+\.(?:jpe?g|png|bmp|gif))["\']', tr, re.I)
            if m:
                img = url_to_path(m.group(1), base)

            disc = discipline_from_nodes(texts)
            e1 = {"id": ids[0], "item": items[0], "level": level, "disc": disc}
            e2 = {"id": ids[1], "item": items[1], "level": level, "disc": disc} if len(ids) > 1 else None
            rows.append(self._make_row(cname, cname, cstatus, dist, level, e1, e2, img))

        if not rows:
            raise Exception("No clash rows with Element IDs found in this HTML file.\n"
                            "Export the Navisworks report as HTML (Tabular) with "
                            "'Element ID' included and images kept next to the file.")
        return rows

    @staticmethod
    def _html_item_names(texts, ids):
        names = ["", ""]
        for n, want in enumerate(ids[:2]):
            found = False
            for i, t in enumerate(texts):
                if want in t and "element id" in t.lower():
                    # scan the next few cells for a plausible item name
                    for j in range(i + 1, min(i + 4, len(texts))):
                        cand = texts[j]
                        if (cand and len(cand) < 60 and ">" not in cand
                                and not re.match(r"^[\d.\-:]+$", cand)
                                and "element id" not in cand.lower()):
                            names[n] = cand
                            found = True
                            break
                if found:
                    break
        return names

    @staticmethod
    def _make_row(cname, cguid, cstatus, dist, level, e1, e2, img):
        disc1 = e1.get("disc") or "?"
        disc2 = (e2.get("disc") if e2 else "") or ""
        disc = disc1 if (not disc2 or disc2 == disc1) else u"{} / {}".format(disc1, disc2)
        return {
            "clash":      cname,
            "clash_guid": cguid,
            "status":     cstatus,
            "distance":   dist,
            "level":      level or "(none)",
            "discipline": disc,
            "id1":        e1.get("id") or "",
            "item1":      e1.get("item") or "",
            "id2":        (e2.get("id") if e2 else "") or "",
            "item2":      (e2.get("item") if e2 else "") or "",
            "image":      img,
            "_in1":       None,
            "_in2":       None,
        }

    def _resolve_all(self):
        """Pre-resolve both elements of every clash once."""
        lab = {"host": "Host", "link": "Link"}
        for r in self.all_rows:
            r["_in1"] = lab.get(self.resolve(r["id1"])["where"], "Not found") if r["id1"] else u"\u2014"
            r["_in2"] = lab.get(self.resolve(r["id2"])["where"], "Not found") if r["id2"] else u"\u2014"

    # ------------------------------------------------------------------ #
    # Config / status persistence
    # ------------------------------------------------------------------ #
    def _stored_path(self):
        try:
            p = cfg.get_option("last_xml", None)
            return p if p else None
        except Exception:
            return None

    def _remember(self, path=None, level=None):
        try:
            if path  is not None: cfg.last_xml   = path
            if level is not None: cfg.last_level = level
            script.save_config()
        except Exception as ex:
            logger.debug("save_config: %s", ex)

    def _status_store_path(self):
        base   = os.getenv("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "ClashNavigator")
        try:
            if not os.path.isdir(folder):
                os.makedirs(folder)
        except Exception:
            folder = base
        key = os.path.splitext(os.path.basename(self.current_report or "report"))[0]
        return os.path.join(folder, key + "__status.json")

    def _load_status_store(self):
        self.status_overrides = {}
        if not self.current_report:
            return
        p = self._status_store_path()
        if os.path.isfile(p):
            try:
                self.status_overrides = json.loads(read_text(p))
            except Exception as ex:
                logger.debug("status load: %s", ex)

    def _save_status_store(self):
        if not self.current_report:
            return
        try:
            write_text(self._status_store_path(),
                       json.dumps(self.status_overrides, ensure_ascii=False))
        except Exception as ex:
            logger.debug("status save: %s", ex)

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    def on_load(self, s, e):
        path = forms.pick_file(
            files_filter="Clash Reports (*.xml;*.html;*.htm)|*.xml;*.html;*.htm|"
                         "XML reports (*.xml)|*.xml|HTML reports (*.html;*.htm)|*.html;*.htm",
            title="Select Navisworks clash report (XML or HTML)")
        if path:
            self._load_report(path, remember=True)

    def on_load_many(self, s, e):
        paths = forms.pick_file(
            files_filter="Clash Reports (*.xml;*.html;*.htm)|*.xml;*.html;*.htm|"
                         "XML reports (*.xml)|*.xml|HTML reports (*.html;*.htm)|*.html;*.htm",
            title="Select one or more Navisworks clash reports (XML or HTML)",
            multi_file=True)
        if not paths:
            return
        # pick_file may return a single string when only one is chosen
        if isinstance(paths, str):
            paths = [paths]

        loaded, failed, first_new = 0, [], None
        for p in paths:
            rows = self._parse_report(p, silent=True)
            if rows is None:
                failed.append(os.path.basename(p))
                continue
            idx = self._add_or_update_report(p, rows)
            if first_new is None:
                first_new = idx
            loaded += 1

        if loaded:
            self._rebuild_report_combo()
            self._activate_report(first_new if first_new is not None else 0)
            self._remember(path=self.reports[self.current_index]["path"])
        msg = "Loaded {} report(s).".format(loaded)
        if failed:
            msg += " Skipped: {}.".format(", ".join(failed))
        self.set_status(msg)

    def on_load_last(self, s, e):
        path = self._stored_path()
        if not path or not os.path.isfile(path):
            forms.alert("Previous report not found.\nUse 'Load Report...' to pick one.")
            self.last_btn.IsEnabled = False
            self.reset_last_btn.IsEnabled = False
            return
        self._load_report(path, remember=False)

    def on_reset_last(self, s, e):
        """Forget the remembered last report (does not touch the loaded data)."""
        self._remember(path="")
        self.last_btn.IsEnabled = False
        self.reset_last_btn.IsEnabled = False
        self.set_status("Last report memory cleared. 'Last Report' is now disabled.")

    # ------------------------------------------------------------------ #
    # Report parsing / management
    # ------------------------------------------------------------------ #
    def _parse_report(self, path, silent=False):
        """Parse a report file into rows. Returns rows list or None on error."""
        if not path or not os.path.isfile(path):
            if not silent:
                forms.alert("File not found:\n\n{}".format(path))
            return None
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in (".html", ".htm"):
                return self.parse_html(path)
            return self.parse_xml(path)
        except Exception as ex:
            if not silent:
                forms.alert("Cannot parse report:\n\n{}".format(ex), title="Parse error")
            logger.debug("parse %s: %s", path, ex)
            return None

    def _add_or_update_report(self, path, rows):
        """Add a report to the list (or replace if same path). Returns its index."""
        name = os.path.basename(path)
        for i, rep in enumerate(self.reports):
            if os.path.normcase(rep["path"]) == os.path.normcase(path):
                rep["rows"] = rows
                return i
        self.reports.append({"path": path, "name": name, "rows": rows})
        return len(self.reports) - 1

    def _rebuild_report_combo(self):
        self._switching = True
        try:
            self.report_cb.Items.Clear()
            for rep in self.reports:
                self.report_cb.Items.Add(u"{}  ({} clashes)".format(rep["name"], len(rep["rows"])))
        finally:
            self._switching = False
        multi = len(self.reports) > 0
        try:
            self.report_bar.Visibility = Visibility.Visible if multi else Visibility.Collapsed
            self.report_count_tb.Text = ("{} reports loaded".format(len(self.reports))
                                         if len(self.reports) > 1 else "")
        except Exception:
            pass

    def _load_report(self, path, remember=False):
        """Load a single report, add it to the list, and make it active."""
        rows = self._parse_report(path, silent=False)
        if rows is None:
            return False
        idx = self._add_or_update_report(path, rows)
        self._rebuild_report_combo()
        self._activate_report(idx)
        if remember:
            self._remember(path=path)
        return True

    def _activate_report(self, index):
        """Make reports[index] the active dataset and refresh the whole UI."""
        if index < 0 or index >= len(self.reports):
            return
        self.current_index  = index
        rep                 = self.reports[index]
        self.current_report = rep["path"]
        self.all_rows       = rep["rows"]

        # reflect selection in the dropdown
        self._switching = True
        try:
            self.report_cb.SelectedIndex = index
        finally:
            self._switching = False

        self._resolve_cache = {}
        self._load_status_store()

        # apply saved statuses
        for r in self.all_rows:
            g = r.get("clash_guid")
            if g in self.status_overrides:
                r["status"] = self.status_overrides[g]

        # pre-resolve elements
        self._resolve_all()

        levels = sorted(set(r["level"] for r in self.all_rows))
        self._loading = True
        self.level_cb.Items.Clear()
        self.level_cb.Items.Add("All Levels")
        for lv in levels:
            self.level_cb.Items.Add(lv)

        want = None
        try:
            want = cfg.get_option("last_level", None)
        except Exception:
            pass
        lidx = self.level_cb.Items.IndexOf(want) if want else -1
        self.level_cb.SelectedIndex = lidx if lidx >= 0 else 0
        self._loading = False

        self.last_btn.IsEnabled = True
        self.reset_last_btn.IsEnabled = True
        self._populate_filters()
        self.apply_filter()
        n_img = sum(1 for r in self.all_rows if r.get("image"))
        self.set_status("Loaded {} clashes across {} levels from {}{}"
                        .format(len(self.all_rows), len(levels), rep["name"],
                                " ({} with images)".format(n_img) if n_img else ""))
        self._update_preview(None)

    def on_report_changed(self, s, e):
        if self._switching:
            return
        idx = self.report_cb.SelectedIndex
        if idx >= 0 and idx != getattr(self, "current_index", -1):
            self._activate_report(idx)
            self._remember(path=self.reports[idx]["path"])

    def on_close_report(self, s, e):
        if not self.reports:
            return
        idx = getattr(self, "current_index", -1)
        if idx < 0 or idx >= len(self.reports):
            return
        name = self.reports[idx]["name"]
        del self.reports[idx]
        if not self.reports:
            # nothing left — clear the grid
            self.all_rows = []
            self.current_report = None
            self.current_index = -1
            self._rebuild_report_combo()
            self.build_table([])
            self.level_cb.Items.Clear()
            self.set_status("Removed '{}'. No reports loaded.".format(name))
            return
        self._rebuild_report_combo()
        self._activate_report(min(idx, len(self.reports) - 1))
        self.set_status("Removed '{}'. {} report(s) remaining.".format(name, len(self.reports)))


    # ------------------------------------------------------------------ #
    # Filters
    # ------------------------------------------------------------------ #
    def _populate_filters(self):
        self._loading = True
        statuses = list(STATUS_OPTIONS)
        for r in self.all_rows:
            if r["status"] and r["status"] not in statuses:
                statuses.append(r["status"])
        discs = set()
        for r in self.all_rows:
            for d in r["discipline"].split(" / "):
                if d:
                    discs.add(d)
        self.f_status.populate(statuses)
        self.f_disc.populate(sorted(discs))
        self.f_inmodel.populate(["Host", "Link", "Not found"])
        self._loading = False

    def on_reset_filters(self, s, e):
        self._loading = True
        self.f_status.reset()
        self.f_disc.reset()
        self.f_inmodel.reset()
        self.search_tb.Text = ""
        self._loading = False
        if self.all_rows:
            self.apply_filter()

    def _apply_if_ready(self):
        if self.all_rows and not self._loading:
            self.apply_filter()

    def _selected_level(self):
        item = self.level_cb.SelectedItem
        return None if (item is None or item == "All Levels") else item

    def on_filter_changed(self, s, e):
        if not self.all_rows or self._loading:
            return
        if s is self.level_cb:
            lvl = self._selected_level()
            self._remember(level=(lvl or "All Levels"))
        self.apply_filter()

    def apply_filter(self):
        level      = self._selected_level()
        term       = (self.search_tb.Text or "").strip().lower()
        sel_status = self.f_status.selected()
        sel_disc   = self.f_disc.selected()
        sel_inmodel= self.f_inmodel.selected()

        rows = []
        for r in self.all_rows:
            if level is not None and r["level"] != level:
                continue
            if sel_status is not None and r["status"] not in sel_status:
                continue
            if sel_disc is not None:
                if not any(d in sel_disc for d in r["discipline"].split(" / ")):
                    continue
            if sel_inmodel is not None:
                if r.get("_in1") not in sel_inmodel and r.get("_in2") not in sel_inmodel:
                    continue
            if term:
                hay = u"{} {} {} {} {}".format(r["id1"], r["id2"], r["item1"],
                                               r["item2"], r["clash"]).lower()
                if term not in hay:
                    continue
            rows.append(r)

        self.build_table(rows)
        self.set_status("Showing {} of {} clashes.".format(len(rows), len(self.all_rows)))

    def build_table(self, rows):
        self._selecting = True
        try:
            t = DataTable()
            for c in ["Clash", "Status", "Level", "Discipline",
                      "Id1", "In1", "Id2", "In2", "Items", "Img", "_key"]:
                t.Columns.Add(c)
            self.visible_rows = rows
            self.table = t
            for i, r in enumerate(rows):
                items = r["item1"]
                if r["item2"]:
                    items = u"{}  |  {}".format(r["item1"] or u"\u2014", r["item2"])
                t.Rows.Add(r["clash"], r["status"], r["level"], r["discipline"],
                           str(r["id1"]), r.get("_in1") or u"\u2014",
                           str(r["id2"]), r.get("_in2") or u"\u2014",
                           items, u"\u2713" if r.get("image") else u"",
                           str(i))
            self.grid.ItemsSource = t.DefaultView
        finally:
            self._selecting = False
        self._update_preview(None)

    def _selected_row(self):
        sel = self.grid.SelectedItem
        if sel is None:
            return None
        try:
            return self.visible_rows[int(sel["_key"])]
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Status editing
    # ------------------------------------------------------------------ #
    def on_cell_edit(self, s, e):
        try:
            from System.Windows.Controls import DataGridEditAction
            try:
                committed = (e.EditAction == DataGridEditAction.Commit)
            except Exception:
                committed = (str(e.EditAction) == "Commit")
            if not committed:
                return
            if str(e.Column.Header) != "Status":
                return
            newval  = e.EditingElement.SelectedItem
            rowview = e.Row.Item
            key     = int(rowview["_key"])
            row     = self.visible_rows[key]
            if newval and newval != row["status"]:
                self._set_clash_status(row.get("clash_guid"), row["clash"], newval)
        except Exception as ex:
            logger.debug("cell edit: %s", ex)

    def _set_clash_status(self, guid, clash_name, newval):
        for r in self.all_rows:
            if (guid and r.get("clash_guid") == guid) or \
               (not guid and r["clash"] == clash_name):
                r["status"] = newval
        if guid:
            self.status_overrides[guid] = newval
            self._save_status_store()
        if self.table:
            for drow in self.table.Rows:
                try:
                    vr = self.visible_rows[int(drow["_key"])]
                    if (guid and vr.get("clash_guid") == guid) or vr["clash"] == clash_name:
                        drow["Status"] = newval
                except Exception:
                    pass
        self.set_status("Clash '{}' -> '{}' saved.".format(clash_name, newval))

    def on_export_status(self, s, e):
        if not self.all_rows:
            forms.alert("Load a report first.")
            return
        default  = os.path.splitext(os.path.basename(self.current_report or "clash"))[0] + "_status.csv"
        savepath = forms.save_file(file_ext="csv", default_name=default)
        if not savepath:
            return
        try:
            lines = [u"Clash,Status,Level,Element ID 1,Element ID 2,GUID"]
            for r in self.all_rows:
                lines.append(u'"{}","{}","{}","{}","{}","{}"'.format(
                    r["clash"], r["status"], r["level"],
                    r["id1"], r["id2"], r.get("clash_guid") or ""))
            write_text(savepath, u"\n".join(lines) + u"\n")
            self.set_status("Exported {} statuses -> {}".format(len(self.all_rows),
                                                                os.path.basename(savepath)))
        except Exception as ex:
            forms.alert("Cannot write CSV:\n\n{}".format(ex))

    def _copy_to_clipboard(self, text):
        """Copy text to the clipboard. Returns True on success."""
        try:
            Clipboard.SetText(text)
            return True
        except Exception as ex:
            logger.debug("clipboard: %s", ex)
            forms.alert("Cannot copy to clipboard:\n\n{}".format(ex))
            return False


    # ------------------------------------------------------------------ #
    # Clash image preview
    # ------------------------------------------------------------------ #
    def _update_preview(self, row):
        img = row.get("image") if row else None
        if img and os.path.isfile(img):
            try:
                bi = BitmapImage()
                bi.BeginInit()
                bi.UriSource = Uri(img, UriKind.Absolute)
                bi.CacheOption = BitmapCacheOption.OnLoad
                bi.EndInit()
                self.preview_img.Source = bi
                self.preview_title.Text = "Clash View — {}".format(row["clash"])
                self.preview_panel.Visibility = Visibility.Visible
                self.open_img_btn.IsEnabled = True
                self._preview_path = img
                return
            except Exception as ex:
                logger.debug("preview: %s", ex)
        self.preview_img.Source = None
        self.preview_panel.Visibility = Visibility.Collapsed
        self.open_img_btn.IsEnabled = False
        self._preview_path = None

    def on_open_image(self, s, e):
        p = getattr(self, "_preview_path", None)
        if p and os.path.isfile(p):
            try:
                os.startfile(p)
            except Exception as ex:
                forms.alert("Cannot open image:\n\n{}".format(ex))

    # ------------------------------------------------------------------ #
    # Row selection / auto-zoom
    # ------------------------------------------------------------------ #
    def on_row_changed(self, s, e):
        if self._selecting:
            return
        row = self._selected_row()
        self._update_preview(row)
        if not self.autozoom_cb.IsChecked:
            return
        if row:
            self._go_to([row["id1"], row["id2"]], zoom=True)

    def on_select_1(self, s, e):
        row = self._selected_row()
        if not row:
            forms.alert("Select a row first.")
            return
        if not row["id1"]:
            forms.alert("This clash has no host-model Element ID.")
            return
        self._go_to([row["id1"]], zoom=True)
        if self._copy_to_clipboard(row["id1"]):
            self.set_status(u"Host ID {} selected + copied to clipboard.".format(row["id1"]))

    def on_select_2(self, s, e):
        row = self._selected_row()
        if not row:
            forms.alert("Select a row first.")
            return
        if not row["id2"]:
            forms.alert("This clash has no linked-model Element ID.")
            return
        self._go_to([row["id2"]], zoom=True)
        if self._copy_to_clipboard(row["id2"]):
            self.set_status(u"Link ID {} selected + copied to clipboard.".format(row["id2"]))

    def on_select_both(self, s, e):
        row = self._selected_row()
        if not row:
            forms.alert("Select a row first.")
            return
        self._go_to([row["id1"], row["id2"]], zoom=True)

    def _go_to(self, eid_strs, zoom=True):
        host_ids       = []
        link_results   = []
        link_for_zoom  = None
        missing        = []

        for eid_str in eid_strs:
            if not eid_str:
                continue
            res = self.resolve(eid_str)
            if res["where"] == "host":
                host_ids.append(res["eid"])
            elif res["where"] == "link":
                link_results.append(res)
                if link_for_zoom is None:
                    link_for_zoom = res
            else:
                missing.append(str(eid_str))

        if not host_ids and not link_results:
            if missing:
                self.set_status("Not found in model: {}".format(", ".join(missing)))
            return

        # run inside Revit's API context (modeless-safe)
        self.run_in_revit(lambda: self._do_select(host_ids, link_results, zoom, link_for_zoom))
        msg = "Selected {} element(s).".format(len(host_ids) + len(link_results))
        if missing:
            msg += " Not found: {}.".format(", ".join(missing))
        self.set_status(msg)

    # ------------------------------------------------------------------ #
    # Highlight / clear
    # ------------------------------------------------------------------ #
    def on_highlight(self, s, e):
        row = self._selected_row()
        if not row:
            forms.alert("Select a row first.")
            return

        host_eids = []
        others    = []
        for eid_str in (row["id1"], row["id2"]):
            if not eid_str:
                continue
            res = self.resolve(eid_str)
            if res["where"] == "host":
                host_eids.append(res["eid"])
            else:
                others.append(eid_str)

        if not host_eids:
            forms.alert("Colour highlight only works for host-model elements.\n"
                        "Both elements are in a link or not found — they will be "
                        "selected/zoomed only.")
            self._go_to([row["id1"], row["id2"]], zoom=True)
            return

        def _do():
            uidoc = ruidoc()
            doc = rdoc()
            if uidoc is None or doc is None:
                return
            view = uidoc.ActiveView
            solid_fill = get_solid_fill_id(doc)
            ogs = DB.OverrideGraphicSettings()
            c = DB.Color(255, 90, 0)
            ogs.SetProjectionLineColor(c)
            ogs.SetCutLineColor(c)
            try:
                ogs.SetProjectionLineWeight(6)
            except Exception:
                pass
            if solid_fill != DB.ElementId.InvalidElementId:
                ogs.SetSurfaceForegroundPatternId(solid_fill)
                ogs.SetSurfaceForegroundPatternColor(c)
                ogs.SetCutForegroundPatternId(solid_fill)
                ogs.SetCutForegroundPatternColor(c)
            t = DB.Transaction(doc, "Highlight clash elements")
            t.Start()
            try:
                for eid in host_eids:
                    view.SetElementOverrides(eid, ogs)
                    self.overridden.append((view.Id, eid))
                t.Commit()
            except Exception as ex:
                try:
                    t.RollBack()
                except Exception:
                    pass
                logger.debug("highlight: %s", ex)
                return
            self._do_select(host_eids, [], True, None)

        self.run_in_revit(_do)
        msg = "Highlighted {} element(s) in orange.".format(len(host_eids))
        if others:
            msg += " Skipped (link/not found): {}.".format(", ".join(others))
        self.set_status(msg)

    def on_clear(self, s, e):
        if not self.overridden:
            self.set_status("Nothing to clear.")
            return
        items = list(self.overridden)

        def _do():
            doc = rdoc()
            if doc is None:
                return
            empty = DB.OverrideGraphicSettings()
            t = DB.Transaction(doc, "Clear clash highlights")
            t.Start()
            try:
                for vid, eid in items:
                    v = doc.GetElement(vid)
                    if v:
                        try:
                            v.SetElementOverrides(eid, empty)
                        except Exception:
                            pass
                t.Commit()
            except Exception as ex:
                try:
                    t.RollBack()
                except Exception:
                    pass
                logger.debug("clear: %s", ex)

        self.run_in_revit(_do)
        self.overridden = []
        self.set_status("Cleared {} highlight(s).".format(len(items)))

    # ------------------------------------------------------------------ #
    def set_status(self, text):
        try:
            self.status_tb.Text = str(text)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Keep a module-level reference so the persistent engine does not collect the
# modeless window or its external event.
_clash_window = None

if __name__ == "__main__":
    if rdoc() is None or ruidoc() is None:
        forms.alert("Open a Revit model first.", exitscript=True)
    _clash_window = ClashWindow()
    _clash_window.show()
