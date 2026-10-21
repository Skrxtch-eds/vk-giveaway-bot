"""Настройки акции. Секреты читаются только из переменных окружения."""
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path


@dataclass(frozen=True)
class Prize:
    name: str
    count: int
    instructions: str


@dataclass(frozen=True)
class PartnerDay:
    date: str
    partner_id: str
    partner_name: str
    group_id: int
    promo_expires: str
    promo_where: str
    draw_at: str
    prizes: tuple[Prize, ...]

    @property
    def url(self) -> str:
        return f"https://vk.com/club{self.group_id}"


@dataclass(frozen=True)
class Campaign:
    title: str
    demo: bool
    utc_offset_hours: int
    rules: str
    consent_text: str
    consent_version: str
    final_prize: str
    final_instructions: str
    final_recipient: str
    final_draw_at: str
    days: tuple[PartnerDay, ...]
    reserve_count: int = 5
    claim_hours: int = 48
    banner_attachment: str = ""

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(hours=self.utc_offset_hours))

    def local_date(self, now: datetime) -> str:
        return now.astimezone(self.tz).date().isoformat()

    def current_day(self, now: datetime) -> PartnerDay | None:
        return next((day for day in self.days if day.date == self.local_date(now)), None)

    def day(self, key: str) -> PartnerDay:
        try:
            return next(day for day in self.days if day.date == key)
        except StopIteration:
            raise ValueError(f"Неизвестный день акции: {key}") from None

    def as_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def moment(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("У времени должен быть указан часовой пояс: например +03:00")
    return result.astimezone(timezone.utc)


def load_campaign(path: str | Path) -> Campaign:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    days = []
    for item in data.pop("days"):
        item["prizes"] = tuple(Prize(**prize) for prize in item["prizes"])
        days.append(PartnerDay(**item))
    campaign = Campaign(days=tuple(days), **data)
    validate(campaign)
    return campaign


def validate(campaign: Campaign) -> None:
    if not campaign.days:
        raise ValueError("Нужно задать хотя бы один партнёрский день")
    if type(campaign.demo) is not bool or type(campaign.utc_offset_hours) is not int:
        raise ValueError("demo должен быть true/false, utc_offset_hours — целым числом")
    if not -12 <= campaign.utc_offset_hours <= 14:
        raise ValueError("Некорректное смещение часового пояса")
    if type(campaign.reserve_count) is not int or campaign.reserve_count < 0:
        raise ValueError("Количество резервных победителей должно быть неотрицательным")
    if type(campaign.claim_hours) is not int or campaign.claim_hours <= 0:
        raise ValueError("Срок ответа должен быть положительным числом часов")
    dates = [date.fromisoformat(day.date) for day in campaign.days]
    if dates != sorted(set(dates)):
        raise ValueError("Даты дней должны быть уникальными и идти по порядку")
    for field in (campaign.title, campaign.rules, campaign.consent_text,
                  campaign.consent_version, campaign.final_prize,
                  campaign.final_instructions, campaign.final_recipient):
        if not isinstance(field, str) or not field.strip():
            raise ValueError("Не заполнены тексты акции, согласия или главного приза")
    for day in campaign.days:
        if not day.partner_id.strip() or not day.partner_name.strip():
            raise ValueError("Не заполнен партнёр")
        if type(day.group_id) is not int or day.group_id <= 0:
            raise ValueError("Для проверки подписки нужен положительный ID сообщества VK")
        if not day.promo_expires.strip() or not day.promo_where.strip():
            raise ValueError("Не заполнены условия использования промокода")
        end = datetime.combine(date.fromisoformat(day.date) + timedelta(days=1),
                               datetime.min.time(), campaign.tz)
        if moment(day.draw_at) < end:
            raise ValueError("Итоги дня можно подвести только после окончания этого дня")
        if not day.prizes:
            raise ValueError("Нужно задать хотя бы один приз партнёра")
        for prize in day.prizes:
            if type(prize.count) is not int or prize.count <= 0 or not prize.name.strip() or not prize.instructions.strip():
                raise ValueError("У приза должны быть название, инструкция и положительное количество")
    if moment(campaign.final_draw_at) < max(moment(day.draw_at) for day in campaign.days):
        raise ValueError("Финальный розыгрыш должен проходить после всех партнёрских дней")
