"""What a registered file is: a type and what it can answer, for `list_files`.

Only Excel exists (`pipeline.SOURCE_KINDS`). The engine reads Excel sources directly because the
read also returns warnings only a workbook has. When a second kind (csv, parquet) arrives,
add its reader here as a dict `kind -> function`; no Protocol until a kind has another shape.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from excel_etl.workbook import Workbook

EXCEL_CAPABILITIES = ("sheets", "regions", "formulas", "merged_cells", "hidden_sheets")


class AssetMetadata(BaseModel):
    """Facts about one registered file. No credential or host path: only what is in the state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    source_type: str
    display_name: str
    sha256: str
    role: str | None
    capabilities: tuple[str, ...]
    details: dict[str, int]

    def line(self) -> str:
        """One text line for `list_files`; capabilities are the same for every file of a type."""
        return f"{self.asset_id}|{self.display_name}|role {self.role or '-'}|{self.source_type}"


def asset_metadata(wb: Workbook, asset_id: str, filename: str, role: str | None) -> AssetMetadata:
    hidden = sum(1 for s in wb.sheets if s.visible != "visible")
    return AssetMetadata(
        asset_id=asset_id,
        source_type="excel",
        display_name=filename,
        sha256=wb.sha256,
        role=role,
        capabilities=EXCEL_CAPABILITIES,
        details={"sheets": len(wb.sheets), "hidden_sheets": hidden},
    )
