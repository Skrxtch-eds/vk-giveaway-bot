"""Тексты и клавиатуры отделены от правил акции."""
import json


MENU = [
    [("Получить промокод", "promo"), ("Участвовать в розыгрыше", "participate")],
    [("Как участвовать", "help"), ("Мои шансы", "chances")],
    [("Правила акции", "rules")],
]


def button(label: str, action: str, **payload) -> dict:
    return {"action": {"type": "text", "label": label,
                       "payload": json.dumps({"action": action, **payload}, ensure_ascii=False)},
            "color": "primary"}


def menu() -> dict:
    return {"one_time": False, "buttons": [[button(label, action) for label, action in row] for row in MENU]}


def reply(text: str, keyboard: dict | None = None, attachment: str = "") -> dict:
    result = {"message": text, "keyboard": keyboard or menu()}
    if attachment:
        result["attachment"] = attachment
    return result


def participation(day) -> dict:
    return {"inline": True, "buttons": [
        [{"action": {"type": "open_link", "label": f"Подписаться на {day.partner_name}"[:40], "link": day.url}}],
        [button("Проверить подписку", "check", day=day.date)],
    ]}


def claim_button(offer_id: int) -> dict:
    return {"inline": True, "buttons": [[button("Получить приз", "claim", offer_id=offer_id)]]}


def consent_buttons(offer_id: int) -> dict:
    return {"inline": True, "buttons": [
        [button("Согласен, получить приз", "consent", offer_id=offer_id)],
        [button("Отмена", "cancel_claim")],
    ]}
