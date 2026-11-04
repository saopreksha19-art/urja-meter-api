from datetime import datetime

from pydantic import BaseModel


class Node(BaseModel):
    code: str | None
    name: str | None


class NetworkPath(BaseModel):
    zone: Node
    circle: Node
    division: Node
    subdivision: Node
    substation: Node
    feeder: Node
    dt: Node


class Location(BaseModel):
    lat: float
    lng: float


class Meter(BaseModel):
    meter_id: str
    serial_no: str
    make: str
    phase_type: str
    install_status: str
    install_type: str
    location: Location
    network: NetworkPath
    distance_km: float | None = None


class Transformer(BaseModel):
    code: str
    name: str
    feeder_code: str
    capacity_kva: float
    meter_count: int


class Reading(BaseModel):
    timestamp: datetime
    kwh_total: float | None
    kvah_total: float | None
    voltage_r: float | None
    kwh_delta: float | None


class DailyConsumption(BaseModel):
    date: str
    kwh: float
    reading_count: int


class TreeNode(BaseModel):
    code: str | None
    name: str | None
    level: str
    meter_count: int
    children: list["TreeNode"]


class DataIssue(BaseModel):
    type: str
    meter_id: str | None
    level: str
    detail: str


class Meta(BaseModel):
    as_of: datetime | None
    stale: bool
    total: int | None = None
    page: int | None = None
    page_size: int | None = None


class MeterResponse(BaseModel):
    data: Meter
    meta: Meta


class MeterList(BaseModel):
    data: list[Meter]
    meta: Meta


class TransformerResponse(BaseModel):
    data: Transformer
    meta: Meta


class TransformerList(BaseModel):
    data: list[Transformer]
    meta: Meta


class ReadingList(BaseModel):
    data: list[Reading]
    meta: Meta


class ConsumptionList(BaseModel):
    data: list[DailyConsumption]
    meta: Meta


class Tree(BaseModel):
    data: list[TreeNode]
    meta: Meta


class IssueList(BaseModel):
    data: list[DataIssue]
    meta: Meta


class Health(BaseModel):
    status: str
    meters_loaded: int
    as_of: datetime | None
    stale: bool


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
