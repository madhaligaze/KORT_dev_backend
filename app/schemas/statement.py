from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class StatementTotals(BaseModel):
    income_total: float = 0.0
    expense_total: float = 0.0
    purchase_total: float = 0.0
    transfer_total: float = 0.0
    topup_total: float = 0.0
    cash_withdrawal_total: float = 0.0


class StatementMetadata(BaseModel):
    source_filename: str
    title: str
    parser_key: str
    account_holder: str | None = None
    card_number: str | None = None
    account_number: str | None = None
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    opening_balance: float | None = None
    closing_balance: float | None = None
    transaction_count: int = 0
    totals: StatementTotals = Field(default_factory=StatementTotals)
    # Как адаптивный разбор решил, какие колонки складывать в приход и расход.
    reading_note: str | None = None
    # Чей счёт: "legal" (ТОО, АО, ИП) или "personal". От этого зависит вид
    # таблицы — см. app/services/legal_statement.py. None — признаков не нашлось.
    holder_kind: str | None = None


class StatementTransaction(BaseModel):
    date: str
    amount: float
    income: float | None = None
    expense: float | None = None
    operation: str
    detail: str
    details_operation: str
    direction: str
    document_number: str | None = None
    operation_datetime: str | None = None
    comment: str | None = None
    currency_op: str | None = None
    processing_date: str | None = None
    raw_counterparty: str | None = None
    note: str | None = None
    flags: list[str] = Field(default_factory=list)
    source: str = "native"
    source_confidence: float | None = None
    corrected: bool = False
    source_cells: dict[str, str] = Field(default_factory=dict)
    category: str | None = None


class AIInsightsSummary(BaseModel):
    """Serialisable result of the local AI engine, stored inside the session payload."""
    summary: str = ""
    category_breakdown: dict[str, float] = Field(default_factory=dict)
    top_merchants: list[dict] = Field(default_factory=list)
    recurring: list[dict] = Field(default_factory=list)
    trend: dict = Field(default_factory=dict)
    anomalies: list[dict] = Field(default_factory=list)
    ai_available: bool = False


class ParsedStatement(BaseModel):
    metadata: StatementMetadata
    transactions: list[StatementTransaction]
    ai_insights: AIInsightsSummary | None = None


class PreviewColumn(BaseModel):
    key: str
    label: str
    kind: str = "text"


class PreviewVariant(BaseModel):
    key: str
    name: str
    description: str
    columns: list[PreviewColumn]
    rows: list[dict[str, str | float | int | None]]
    group: str = "primary"
    template_id: str | None = None
    base_variant_key: str | None = None


class PreferenceRecord(BaseModel):
    parser_key: str
    preferred_variant_key: str
    always_show_alternatives: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PreviewResponse(BaseModel):
    session_id: str
    document: StatementMetadata
    parser_matches: list["ParserMatch"] = Field(default_factory=list)
    applied_rule: "AppliedRuleInfo | None" = None
    ocr_review: "OCRReviewPayload | None" = None
    variants: list[PreviewVariant]
    saved_variants: list[PreviewVariant] = Field(default_factory=list)
    templates: list["TransformationTemplate"] = Field(default_factory=list)
    preference: PreferenceRecord | None = None
    default_variant_key: str | None = None
    ai_insights: "AIInsightsSummary | None" = None


class SavePreferenceRequest(BaseModel):
    parser_key: str
    preferred_variant_key: str
    always_show_alternatives: bool = False


class ExportRequest(BaseModel):
    session_id: str
    variant_key: str
    excluded_rows: list[int] = []
    custom_columns: list[dict] | None = None
    custom_rows: list[dict] | None = None


class ExportCsvRequest(BaseModel):
    session_id: str
    variant_key: str


class ComputeRequest(BaseModel):
    """Live recompute of formula columns for the in-browser editor."""
    session_id: str
    variant_key: str
    custom_columns: list[dict] | None = None
    custom_rows: list[dict] | None = None
    excluded_rows: list[int] = []


class ComputeResponse(BaseModel):
    columns: list[PreviewColumn]
    rows: list[dict]


class UpdateRowRequest(BaseModel):
    date: str | None = None
    amount: float | None = None
    operation: str | None = None
    detail: str | None = None
    direction: str | None = None
    note: str | None = None


class SessionSummary(BaseModel):
    session_id: str
    parser_key: str
    source_filename: str
    title: str
    account_holder: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    transaction_count: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ParserDescriptor(BaseModel):
    key: str
    label: str
    description: str
    accepted_extensions: list[str]


class ParserMatch(BaseModel):
    key: str
    label: str
    score: float
    matched: bool = False


class VisionStatus(BaseModel):
    available: bool = False
    backend: str = "disabled"
    ocr_available: bool = False
    ocr_backend: str = "disabled"
    note: str = ""
    use_cases: list[str] = Field(default_factory=list)


class OCRReviewTable(BaseModel):
    table_index: int
    rows: list[list[str]] = Field(default_factory=list)
    suggested_header_row_index: int | None = None
    cell_confidence: list[list[float | None]] = Field(default_factory=list)


class OCRReviewField(BaseModel):
    key: str
    label: str
    required: bool = False


class OCRReviewPayload(BaseModel):
    review_id: str
    source_filename: str
    lines: list[str] = Field(default_factory=list)
    tables: list[OCRReviewTable] = Field(default_factory=list)
    suggested_table_index: int | None = None
    suggested_header_row_index: int | None = None
    available_fields: list[OCRReviewField] = Field(default_factory=list)


class MaterializeOCRReviewRequest(BaseModel):
    table_index: int
    header_row_index: int
    title: str | None = None
    column_mapping: dict[str, int | None] = Field(default_factory=dict)
    save_mapping_template: bool = False
    mapping_template_name: str | None = None


class OCRMappingTemplate(BaseModel):
    template_id: str
    name: str
    version: int = 1
    parser_key: str = "ocr_scanned_statement"
    status: str = "active"
    is_active: bool = True
    header_signature: list[str] = Field(default_factory=list)
    title_keywords: list[str] = Field(default_factory=list)
    column_mapping: dict[str, int | None] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OCRMappingTemplateMatch(BaseModel):
    template_id: str
    name: str
    version: int = 1
    score: float
    header_score: float = 0.0
    title_score: float = 0.0
    table_index: int
    header_row_index: int
    matched_header_signature: list[str] = Field(default_factory=list)
    matched_title_keywords: list[str] = Field(default_factory=list)


class AppliedRuleInfo(BaseModel):
    rule_type: str
    template_id: str
    name: str
    version: int
    score: float
    reason: str
    header_score: float = 0.0
    title_score: float = 0.0
    matched_header_signature: list[str] = Field(default_factory=list)
    matched_title_keywords: list[str] = Field(default_factory=list)


class TemplateColumnConfig(BaseModel):
    key: str
    label: str
    kind: str = "text"
    enabled: bool = True
    formula: str | None = None        # e.g. "{amount} * 0.12"
    source_field: str | None = None   # direct mapping to transaction field
    ai_description: str | None = None # human-readable explanation of this column's logic


class TransformationTemplate(BaseModel):
    template_id: str
    parser_key: str
    name: str
    description: str = ""
    base_variant_key: str
    columns: list[TemplateColumnConfig]
    is_default: bool = False
    # Normalised tokens of the source statement's columns — used to auto-match this
    # template to a freshly parsed statement with the same column structure.
    source_signature: list[str] = Field(default_factory=list)
    # Presentation snapshot for the Web-Excel view (column widths, number formats,
    # styles, frozen header) so the statement is shown exactly as last edited.
    layout: dict | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CreateTemplateRequest(BaseModel):
    parser_key: str
    name: str
    description: str = ""
    base_variant_key: str
    columns: list[TemplateColumnConfig]
    is_default: bool = False
    # When provided, the backend derives source_signature from this session's base
    # variant (more reliable than trusting the client).
    session_id: str | None = None
    source_signature: list[str] = Field(default_factory=list)
    layout: dict | None = None


class UpdateTemplateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    columns: list[TemplateColumnConfig] | None = None
    is_default: bool | None = None
    layout: dict | None = None


class WebExcelSaveRequest(BaseModel):
    """Save a Web-Excel "образ": infer reusable {field} rules from the grid and
    persist them (+ presentation layout) as a TransformationTemplate."""
    session_id: str
    base_variant_key: str
    name: str
    columns: list[dict]                       # ordered [{key, label, kind}]
    rows: list[dict] = Field(default_factory=list)
    a1_formulas: dict[str, str] | None = None
    layout: dict | None = None
    is_default: bool = True


class CorrectionMemoryEntry(BaseModel):
    correction_id: int
    parser_key: str
    field_name: str
    original_value: str
    corrected_value: str
    frequency: int
    last_seen_at: str


class JobSummary(BaseModel):
    job_id: str
    job_type: str
    status: str
    source_filename: str | None = None
    session_id: str | None = None
    review_id: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    payload: dict = Field(default_factory=dict)
    result_payload: dict | None = None


class CreatePreviewJobResponse(BaseModel):
    job_id: str
    status: str


class OCRRuleVersionDiff(BaseModel):
    from_template_id: str
    to_template_id: str
    added_header_tokens: list[str] = Field(default_factory=list)
    removed_header_tokens: list[str] = Field(default_factory=list)
    added_title_keywords: list[str] = Field(default_factory=list)
    removed_title_keywords: list[str] = Field(default_factory=list)
    changed_columns: list[str] = Field(default_factory=list)


class UpdateOCRRuleRequest(BaseModel):
    is_active: bool


# ── Smart Brain schemas ────────────────────────────────────────────────────────

class ColumnRecommendation(BaseModel):
    formula: str
    explanation: str
    confidence: float              # 0.0 – 1.0
    category: str                  # "formula" | "mapping" | "aggregate" | "warning"
    source: str = "lexical"        # "lexical" | "pattern" | "consistency"


class AdvisorColumnRequest(BaseModel):
    column_name: str
    parser_key: str
    available_fields: list[str] = Field(default_factory=list)
    # Optionally pass sample values so pattern_detector can run
    sample_values: list[float | None] = Field(default_factory=list)
    context_columns: dict[str, list[float | None]] = Field(default_factory=dict)


class AdvisorColumnResponse(BaseModel):
    recommendations: list[ColumnRecommendation]


class DiffFinding(BaseModel):
    type: str           # "label_change" | "column_removed" | "column_added" |
                        #  "cell_changed" | "row_removed" | "formula_detected" | "filter_detected"
    column_key: str | None = None
    detected_formula: str | None = None
    confidence: float = 1.0
    explanation_ru: str
    intent: str | None = None            # smart NLP intent id, e.g. "fx_convert"
    needs_clarification: bool = False    # True → render as clarify chips in UI


class AnalyzeDiffRequest(BaseModel):
    session_id: str
    original_variant_key: str
    edited_columns: list[dict]
    edited_rows: list[dict]


class AnalyzeDiffResponse(BaseModel):
    findings: list[DiffFinding]
    summary_ru: str


class ReAnalyzeRequest(BaseModel):
    session_id: str
    original_variant_key: str
    edited_columns: list[dict]
    edited_rows: list[dict]
    user_hint: str
    target_column_key: str | None = None     # column the user clicked "Уточнить" on
    existing_findings: list[DiffFinding] | None = None  # prior analysis results


class FormulaPatch(BaseModel):
    column_key: str
    formula: str | None = None
    source_field: str | None = None
    new_label: str | None = None
    after_key: str | None = None
    filter_direction: str | None = None
    filter_threshold: float | None = None
    filter_keyword: str | None = None


class ClarifyQuestion(BaseModel):
    question_ru: str
    choices: list[str] = Field(default_factory=list)
    choice_formulas: list[str] = Field(default_factory=list)
    column_key: str | None = None


class SmartRefineRequest(BaseModel):
    session_id: str
    original_variant_key: str
    edited_columns: list[dict]
    edited_rows: list[dict]
    user_hint: str
    target_column_key: str | None = None
    existing_findings: list[DiffFinding] | None = None


class SmartRefineResponse(BaseModel):
    findings: list[DiffFinding]
    narrative_ru: str
    clarifications: list[ClarifyQuestion] = Field(default_factory=list)
    confidence: float
    summary_ru: str


class ClarifyRequest(BaseModel):
    session_id: str
    original_variant_key: str
    edited_columns: list[dict]
    edited_rows: list[dict]
    user_hint: str
    target_column_key: str | None = None
    existing_findings: list[DiffFinding] | None = None
    choice_index: int
    question_ru: str


class ConsistencyWarning(BaseModel):
    type: str           # "balance_mismatch" | "duplicate_row" | "date_gap" | "anomaly" | "currency"
    severity: str       # "low" | "medium" | "high"
    message_ru: str
    affected_rows: list[int] = Field(default_factory=list)
    affected_column: str | None = None


class ConsistencyReport(BaseModel):
    warnings: list[ConsistencyWarning]
    is_clean: bool


class OCRRuleManagerSnapshot(BaseModel):
    templates: list[OCRMappingTemplate] = Field(default_factory=list)
    grouped_versions: dict[str, list[OCRMappingTemplate]] = Field(default_factory=dict)


class OnboardingSample(BaseModel):
    sample_id: str
    project_id: str
    source_filename: str
    review_id: str | None = None
    session_id: str | None = None
    status: str = "captured"
    payload: dict | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OnboardingProject(BaseModel):
    project_id: str
    name: str
    bank_name: str
    status: str = "draft"
    notes: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    samples: list[OnboardingSample] = Field(default_factory=list)


class CreateOnboardingProjectRequest(BaseModel):
    name: str
    bank_name: str
    notes: str | None = None


class AddOnboardingSampleRequest(BaseModel):
    source_filename: str
    review_id: str | None = None
    session_id: str | None = None
    status: str = "captured"
    payload: dict | None = None
