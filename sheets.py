"""Reading a partner spreadsheet - CSV or XLSX - with no third-party parser.

The dashboard has to accept whatever a partner actually sends, which is a .csv
from one and a .xlsx from the next. openpyxl would read the second, but this
project deliberately ships six dependencies and runs on boxes where `pip
install` is not always available, so the XLSX reader here is built on zipfile +
ElementTree from the standard library.

That is a fair trade because we need very little: an .xlsx is a zip of XML, and
a sheet of tour rows uses none of the hard parts (no formulas to evaluate, no
pivot caches, no charts). What it does need - shared strings, inline strings,
date serials and sparse rows - is handled below.

Column names are guessed, never demanded. Partners label the same column
"Tour ID", "product_id" or "Ref", and refusing the file until someone renames a
header would mean the feature goes unused. `detect_columns` maps what it finds
and reports what it mapped, so the UI can show the user which column became
which field and let them correct it.
"""
import csv
import datetime as dt
import io
import re
import zipfile
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree

# The canonical fields a comparison can use. Everything else in the sheet is
# kept in `raw` and shown on demand, but never matched on.
FIELDS = ("external_id", "title", "start_date", "end_date", "venue", "url")

# Header text -> canonical field. Matched case-insensitively against the header
# with punctuation stripped, longest pattern first, so "tour name" beats "tour".
#
# Order within each list matters: the first pattern that matches a header wins,
# and each field claims at most one column (the first one that matched it).
HEADER_PATTERNS: List[Tuple[str, List[str]]] = [
    ("url", [
        "partnerurl", "producturl", "toururl", "eventurl", "bookingurl",
        "link", "url", "weblink", "permalink",
    ]),
    ("external_id", [
        "externalid", "partnerid", "productid", "tourid", "eventid", "activityid",
        "productcode", "tourcode", "reference", "refno", "ref", "uid", "guid",
        "sku", "code", "id",
    ]),
    ("title", [
        "tourname", "productname", "eventname", "activityname", "tourtitle",
        "eventtitle", "producttitle", "title", "name", "tour", "event",
        "product", "activity", "description",
    ]),
    ("start_date", [
        "startdate", "startdatetime", "starttime", "datefrom", "fromdate",
        "begindate", "dates", "start", "date", "from", "departure",
    ]),
    ("end_date", [
        "enddate", "enddatetime", "endtime", "dateto", "todate", "finishdate",
        "enddates", "end", "until", "to",
    ]),
    ("venue", [
        "venuename", "locationname", "venue", "location", "place", "city",
        "address", "destination",
    ]),
]

# Date formats seen in these sheets, tried in order. Day-first before month-first
# because the partner sheets in this project are written that way; an ambiguous
# 03/04/2026 is therefore 3 April, and `parse_date` reports which rule it used
# so the UI can say so rather than letting the user assume.
DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
    "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
    "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
    "%m/%d/%Y", "%m-%d-%Y",
    "%d-%m-%y", "%d/%m/%y", "%m/%d/%y",
]

# Excel stores a date as days since 1899-12-30 (the offset absorbs Excel's
# deliberate 1900 leap-year bug, which it keeps for Lotus compatibility).
EXCEL_EPOCH = dt.date(1899, 12, 30)

# Above this many rows a single upload is rejected rather than quietly truncated.
# 200k covers every partner sheet in this project with room to spare; the cap
# exists so a mis-picked file cannot exhaust memory.
MAX_ROWS = 200_000


class SheetError(ValueError):
    """The file could not be read as a spreadsheet."""


def _norm_header(text: str) -> str:
    """'Tour ID (partner)' -> 'tourid'. Punctuation and spacing carry no meaning."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def detect_columns(headers: List[str]) -> Dict[str, Optional[str]]:
    """Map canonical field -> the header it should come from.

    Returns a full mapping with None for anything the sheet does not have, so
    the caller can see what is missing without probing for keys.
    """
    normalised = [(h, _norm_header(h)) for h in headers]
    mapping: Dict[str, Optional[str]] = {f: None for f in FIELDS}
    claimed: set = set()

    for field, patterns in HEADER_PATTERNS:
        for pattern in patterns:
            for original, norm in normalised:
                if original in claimed or not norm:
                    continue
                # Exact match first, then containment - "partner tour id"
                # contains "tourid" once punctuation is stripped.
                if norm == pattern or pattern in norm:
                    mapping[field] = original
                    claimed.add(original)
                    break
            if mapping[field]:
                break
    return mapping


def parse_date(value: Any) -> Optional[str]:
    """Normalise a spreadsheet date to 'YYYY-MM-DD', or None if it isn't one.

    Returning None rather than a guess matters: a date that silently parses
    wrong turns a matching tour into a missing one, which is worse than a blank.
    """
    if value is None:
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return (value.date() if isinstance(value, dt.datetime) else value).isoformat()

    text = str(value).strip()
    if not text:
        return None

    # An Excel serial arrives as a bare number once the cell has a date format.
    if re.fullmatch(r"\d{4,6}(\.\d+)?", text):
        try:
            serial = float(text)
            if 1 <= serial <= 200_000:
                return (EXCEL_EPOCH + dt.timedelta(days=int(serial))).isoformat()
        except (ValueError, OverflowError):
            pass

    # Drop a time portion - comparison is by day, and the two sides rarely
    # agree on the clock even when they agree on the date.
    text = re.split(r"[T ]", text)[0].strip() if re.search(r"\d[T ]\d", text) else text
    text = text.strip()

    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalise_title(value: Any) -> str:
    """Collapse a title to something two systems can agree on.

    Case, punctuation and repeated whitespace all differ freely between a
    partner sheet and our database for what is plainly the same tour, so none of
    them may take part in the comparison.
    """
    text = str(value or "").lower()
    text = re.sub(r"&(amp|nbsp|quot|#39|apos);", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalise_url(value: Any) -> str:
    """Reduce a URL to the part that identifies the product.

    Scheme, host casing, www, trailing slashes and query strings all vary
    between the sheet and `partner_url` in our database while pointing at the
    same tour, so they are stripped.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text)
    text = text.split("?")[0].split("#")[0]
    return text.rstrip("/")


# --------------------------------------------------------------------- CSV


def _read_csv(data: bytes) -> Tuple[List[str], List[Dict[str, Any]]]:
    # utf-8-sig strips the BOM Excel writes on "CSV UTF-8" export; cp1252 is the
    # fallback because that is what a Windows Excel "CSV" actually contains.
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SheetError("could not decode the file as text")

    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        # A single-column sheet gives the sniffer nothing to work with, which is
        # not an error - it just means commas.
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    try:
        headers = next(reader)
    except StopIteration:
        raise SheetError("the file is empty")

    headers = [h.strip() for h in headers]
    rows = []
    for values in reader:
        if not any(str(v).strip() for v in values):
            continue                       # blank separator row
        if len(rows) >= MAX_ROWS:
            raise SheetError(f"more than {MAX_ROWS:,} rows - split the file")
        rows.append({h: (values[i] if i < len(values) else "")
                     for i, h in enumerate(headers)})
    return headers, rows


# -------------------------------------------------------------------- XLSX

_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def _col_index(cell_ref: str) -> int:
    """'AB12' -> 27. Column letters are base-26 with no zero."""
    letters = "".join(c for c in cell_ref if c.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - ord("A") + 1)
    return index - 1


def _shared_strings(zf: zipfile.ZipFile) -> List[str]:
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []                          # a sheet of only numbers has none
    root = ElementTree.fromstring(raw)
    strings = []
    for si in root.findall("main:si", _NS):
        # Rich text splits one string across several <t> runs; joining them is
        # the whole content of that cell.
        strings.append("".join(t.text or "" for t in si.iter(f"{{{_NS['main']}}}t")))
    return strings


def _first_sheet_path(zf: zipfile.ZipFile) -> str:
    """The first sheet in tab order, which is where partners put the data.

    Resolved through the workbook relationships rather than assuming
    'xl/worksheets/sheet1.xml': the first tab is not always sheet1.xml once a
    workbook has had sheets deleted or reordered.
    """
    try:
        workbook = ElementTree.fromstring(zf.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        return "xl/worksheets/sheet1.xml"

    sheet = workbook.find("main:sheets/main:sheet", _NS)
    if sheet is None:
        return "xl/worksheets/sheet1.xml"
    rel_id = sheet.get(f"{{{_NS['rel']}}}id")

    for rel in rels:
        if rel.get("Id") == rel_id:
            target = rel.get("Target", "")
            return target[1:] if target.startswith("/") else f"xl/{target.lstrip('/')}"
    return "xl/worksheets/sheet1.xml"


def _date_styles(zf: zipfile.ZipFile) -> set:
    """Style indices whose number format is a date.

    Without this a date cell is just a float, and every date in the sheet would
    come back as '45678'. Excel's built-in date formats are ids 14-22 and 45-47;
    custom ones are recognised by their format code containing y/d and not being
    a colour or text section.
    """
    try:
        styles = ElementTree.fromstring(zf.read("xl/styles.xml"))
    except KeyError:
        return set()

    date_formats = set(range(14, 23)) | {27, 30, 36, 45, 46, 47, 50, 57}
    for fmt in styles.findall("main:numFmts/main:numFmt", _NS):
        code = (fmt.get("formatCode") or "").lower()
        if re.search(r"[dy]", code) and "[" not in code:
            try:
                date_formats.add(int(fmt.get("numFmtId")))
            except (TypeError, ValueError):
                continue

    styled = set()
    cell_xfs = styles.find("main:cellXfs", _NS)
    if cell_xfs is not None:
        for index, xf in enumerate(cell_xfs.findall("main:xf", _NS)):
            try:
                if int(xf.get("numFmtId", 0)) in date_formats:
                    styled.add(index)
            except (TypeError, ValueError):
                continue
    return styled


def _read_xlsx(data: bytes) -> Tuple[List[str], List[Dict[str, Any]]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SheetError("not a readable .xlsx file (it may be an old .xls)")

    with zf:
        strings = _shared_strings(zf)
        date_styles = _date_styles(zf)
        try:
            sheet_xml = zf.read(_first_sheet_path(zf))
        except KeyError:
            raise SheetError("the workbook contains no readable sheet")

        headers: List[str] = []
        rows: List[Dict[str, Any]] = []

        for row_el in ElementTree.fromstring(sheet_xml).iter(f"{{{_NS['main']}}}row"):
            values: Dict[int, Any] = {}
            for cell in row_el.findall("main:c", _NS):
                index = _col_index(cell.get("r", "A1"))
                cell_type = cell.get("t")

                if cell_type == "inlineStr":
                    node = cell.find("main:is", _NS)
                    text = "".join(t.text or "" for t in node.iter(f"{{{_NS['main']}}}t")) if node is not None else ""
                else:
                    v = cell.find("main:v", _NS)
                    if v is None or v.text is None:
                        continue
                    text = v.text
                    if cell_type == "s":
                        try:
                            text = strings[int(text)]
                        except (ValueError, IndexError):
                            pass
                    elif cell_type != "str":
                        # Numeric. Convert to a date only if the style says so.
                        try:
                            if int(cell.get("s", -1)) in date_styles:
                                text = (EXCEL_EPOCH + dt.timedelta(days=int(float(text)))).isoformat()
                        except (ValueError, OverflowError):
                            pass
                if str(text).strip():
                    values[index] = text

            if not values:
                continue                    # entirely blank row

            if not headers:
                width = max(values) + 1
                headers = [str(values.get(i, f"column_{i + 1}")).strip() for i in range(width)]
                continue

            if len(rows) >= MAX_ROWS:
                raise SheetError(f"more than {MAX_ROWS:,} rows - split the file")
            rows.append({h: values.get(i, "") for i, h in enumerate(headers)})

    if not headers:
        raise SheetError("the first sheet has no header row")
    return headers, rows


# ------------------------------------------------------------------ public


class ParsedSheet:
    def __init__(self, headers: List[str], rows: List[Dict[str, Any]]):
        self.headers = headers
        self.rows = rows
        self.mapping = detect_columns(headers)

    @property
    def matchable(self) -> bool:
        """Is there enough here to compare on at all?

        A URL or an id gives an exact match. A title alone still works - it is
        weaker, and the UI says so - but a sheet with neither cannot be compared
        against anything and is rejected at upload rather than producing a
        comparison where every row is 'missing'.
        """
        m = self.mapping
        return bool(m["url"] or m["external_id"] or m["title"])

    def normalised(self) -> List[Dict[str, Any]]:
        """Every row reduced to the canonical fields, with the original kept."""
        m = self.mapping
        out = []
        for raw in self.rows:
            out.append({
                "external_id": str(raw.get(m["external_id"], "") or "").strip() if m["external_id"] else "",
                "title": str(raw.get(m["title"], "") or "").strip() if m["title"] else "",
                "start_date": parse_date(raw.get(m["start_date"])) if m["start_date"] else None,
                "end_date": parse_date(raw.get(m["end_date"])) if m["end_date"] else None,
                "venue": str(raw.get(m["venue"], "") or "").strip() if m["venue"] else "",
                "url": str(raw.get(m["url"], "") or "").strip() if m["url"] else "",
                "raw": raw,
            })
        return out


def parse(filename: str, data: bytes) -> ParsedSheet:
    """Read an uploaded partner sheet. Raises SheetError with a readable reason."""
    if not data:
        raise SheetError("the file is empty")

    name = (filename or "").lower()
    if name.endswith(".xlsx") or data[:2] == b"PK":
        headers, rows = _read_xlsx(data)
    elif name.endswith(".xls"):
        # Genuinely a different format - a BIFF binary, not a zip. Saying so is
        # more use than failing to unzip it.
        raise SheetError("old .xls files are not supported - re-save as .xlsx or .csv")
    else:
        headers, rows = _read_csv(data)

    if not rows:
        raise SheetError("the sheet has a header row but no data rows")
    return ParsedSheet(headers, rows)
