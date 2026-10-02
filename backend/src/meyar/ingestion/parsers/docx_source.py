"""Pinned python-docx 1.2.0 adapter: all private/OOXML parsing lives here.

Walk physical source cells, never the layout grid or vMerge origin resolution.
Eight table levels bound provenance; the aggregate XML-node cap also bounds
empty/unsupported input. Library materialization remains in the isolated worker.
"""

from collections.abc import Iterator

from docx.document import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from meyar.ingestion.parser import (
    DOCX_WARNING_CODES,
    BodySource,
    CanonicalBlock,
    DocxWarning,
    ParseError,
    ParseFailureCode,
    SourceCell,
    TableSource,
)
from meyar.ingestion.parser_output import TextBudget
from meyar.ingestion.parser_policy import MAX_DOCX_SOURCE_NODES, MAX_DOCX_TABLE_DEPTH


class SourceBudget:
    def __init__(self) -> None:
        self.nodes = 0

    def inspect(self, root, warnings: set[DocxWarning], *, omitted: DocxWarning | None = None):
        # Iterator stack avoids recursion and avoids a width-sized pending list.
        stack = [(iter((root,)), False, False)]
        while stack:
            element = next(stack[-1][0], None)
            if element is None:
                stack.pop()
                continue
            self.nodes += 1
            if self.nodes > MAX_DOCX_SOURCE_NODES:
                raise ParseError(ParseFailureCode.PARSER_OUTPUT_LIMIT)
            in_textbox = stack[-1][1] or element.tag == qn("w:txbxContent")
            in_ambiguous_merge = stack[-1][2] or (
                omitted is None and element.tag == qn("w:tc") and element.vMerge == "continue"
            )
            if element.tag in (qn("w:t"), qn("w:delText")) and (element.text or "").strip():
                if omitted is not None:
                    warnings.add(omitted)
                if in_textbox:
                    warnings.add("DOCX_TEXTBOX_TEXT_OMITTED")
                if in_ambiguous_merge:
                    warnings.add("DOCX_AMBIGUOUS_MERGE_TEXT_OMITTED")
            stack.append((iter(element), in_textbox, in_ambiguous_merge))


def _row_complete(row, warnings: set[DocxWarning], ambiguous: bool) -> bool:
    """Inspect, never extract, text outside Paragraph.text's supported paths.

    Full XML was already node-budget checked. This second iterator pass skips
    direct nested tables: their own physical rows are inspected independently.
    Unsupported nested wrappers belong to this row and cannot hide text.
    """
    transitions = {
        "row": {qn("w:tc"): "cell"},
        "cell": {qn("w:p"): "paragraph"},
        "paragraph": {qn("w:r"): "run", qn("w:hyperlink"): "hyperlink"},
        "hyperlink": {qn("w:r"): "run"},
        "run": {qn("w:t"): "text"},
    }
    complete = True
    stack = [(iter(row), "row", False, ambiguous)]
    while stack:
        element = next(stack[-1][0], None)
        if element is None:
            stack.pop()
            continue
        _, parent, parent_box, parent_merge = stack[-1]
        if parent == "cell" and element.tag == qn("w:tbl"):
            continue
        state = transitions.get(parent, {}).get(element.tag, "unsupported")
        in_box = parent_box or element.tag == qn("w:txbxContent")
        in_merge = parent_merge or (
            element.tag == qn("w:tc") and element.vMerge == "continue"
        )
        if (
            element.tag in (qn("w:t"), qn("w:delText"))
            and (element.text or "").strip()
            and (state != "text" or in_merge)
        ):
            complete = False
            if in_box:
                warnings.add("DOCX_TEXTBOX_TEXT_OMITTED")
            if in_merge:
                warnings.add("DOCX_AMBIGUOUS_MERGE_TEXT_OMITTED")
            if not in_box and not in_merge:
                warnings.add("DOCX_TABLE_TEXT_OMITTED")
        stack.append((iter(element), state, in_box, in_merge))
    return complete


def _source_items(
    container: Document | _Cell,
    path: list[SourceCell],
    warnings: set[DocxWarning],
    ambiguous: bool = False,
    row_complete: bool = True,
) -> Iterator[tuple[Paragraph, BodySource | TableSource, bool]]:
    """Recursion is confined to at most eight supported table levels."""
    table_ordinal = paragraph_ordinal = 0
    for item in container.iter_inner_content():
        if isinstance(item, Paragraph):
            source = (
                TableSource(
                    path=path, paragraph=paragraph_ordinal, row_context_complete=row_complete
                ) if path else BodySource()
            )
            paragraph_ordinal += 1
            yield item, source, ambiguous
        elif isinstance(item, Table):
            if len(path) >= MAX_DOCX_TABLE_DEPTH:
                raise ParseError(ParseFailureCode.PARSER_OUTPUT_LIMIT)
            # No public row.cells / table.cell lookup: gridSpan never expands work.
            for row_ordinal, row in enumerate(item._tbl.tr_lst):
                complete = _row_complete(row, warnings, ambiguous)
                for cell_ordinal, tc in enumerate(row.tc_lst):
                    step = SourceCell(table=table_ordinal, row=row_ordinal, cell=cell_ordinal)
                    continuation = tc.vMerge == "continue"
                    yield from _source_items(
                        _Cell(tc, item), path + [step], warnings,
                        ambiguous or continuation, complete
                    )
            table_ordinal += 1


def extract_body(
    document: Document, budget: TextBudget
) -> tuple[list[CanonicalBlock], list[DocxWarning]]:
    warnings: set[DocxWarning] = set()
    source_budget = SourceBudget()
    source_budget.inspect(document.element, warnings)
    # Existing relationships only. No section.header/footer accessors, inheritance
    # lookup or definition creation. Shared parts inspected once, all variants count.
    seen: set[int] = set()
    for rel in document.part.rels.values():
        if rel.is_external or rel.reltype not in (RT.HEADER, RT.FOOTER):
            continue
        part = rel.target_part
        if id(part) in seen:
            continue
        seen.add(id(part))
        code: DocxWarning = (
            "DOCX_HEADER_TEXT_OMITTED" if rel.reltype == RT.HEADER else "DOCX_FOOTER_TEXT_OMITTED"
        )
        source_budget.inspect(part.element, warnings, omitted=code)
    blocks = []
    for index, (paragraph, source, ambiguous) in enumerate(_source_items(document, [], warnings)):
        text = paragraph.text.strip()
        if not text:
            continue
        if ambiguous:
            warnings.add("DOCX_AMBIGUOUS_MERGE_TEXT_OMITTED")
            continue
        budget.add(text)
        blocks.append(CanonicalBlock(index=index, text=text, source=source))
    return blocks, [code for code in DOCX_WARNING_CODES if code in warnings]
