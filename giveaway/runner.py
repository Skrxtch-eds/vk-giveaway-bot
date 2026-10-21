"""Приём Long Poll и фоновая обработка очереди; работа восстанавливается из SQLite."""
import logging
import threading

from . import messages
from .outbox import OutboxWorker
from .service import utcnow
from .vk import TransportError, VkError


logger = logging.getLogger(__name__)


def run(bot, service, vk, admin_ids) -> None:
    stop = threading.Event()
    worker = OutboxWorker(service, vk)

    def background():
        while not stop.is_set():
            try:
                now = utcnow()
                service.tick(now)
                with service.db.read() as conn:
                    pending_draws = [row[0] for row in conn.execute("SELECT draw_id FROM draws WHERE status='draft'")]
                # Находим и старые черновики: уведомление администратора не теряется при перезапуске.
                for draw_id in pending_draws:
                    for admin_id in admin_ids:
                        with service.db.transaction():
                            service.touch_user(admin_id, now, started=False)
                            service.enqueue(admin_id, messages.reply(
                                f"Розыгрыш {draw_id} проведён.\nПроверка: /draw {draw_id}\n"
                                f"Подтверждение и отправка уведомлений: /approve {draw_id}"),
                                f"admin-draw:{draw_id}:{admin_id}", now)
                if not worker.send_one():
                    stop.wait(1)
            except Exception as error:
                # Пишем тип, но не запросы, сообщения пользователей и токены.
                logger.error("Ошибка фоновой обработки: %s", type(error).__name__)
                stop.wait(5)

    thread = threading.Thread(target=background, name="outbox", daemon=True)
    thread.start()
    server = None
    with service.db.read() as conn:
        stored = conn.execute("SELECT value FROM settings WHERE key='longpoll_ts'").fetchone()
        ts = stored[0] if stored else None
    try:
        logger.info("Бот запущен. Ожидание сообщений VK.")
        while not stop.is_set():
            try:
                if server is None:
                    server = vk.long_poll_server()
                    ts = ts or str(server["ts"])
                batch = vk.poll(server, ts)
                failed = batch.get("failed")
                if failed == 1:
                    ts = str(batch["ts"])
                    logger.warning("Часть старых событий Long Poll недоступна; позиция обновлена")
                    continue
                if failed in (2, 3):
                    server = None
                    if failed == 3:
                        ts = None
                    continue
                if failed:
                    raise TransportError("Неизвестная ошибка Long Poll")
                for event in batch.get("updates", []):
                    bot.handle(event)
                # Сохраняем позицию только после успешной обработки всего пакета.
                # При сбое пакет повторится; incoming_events защитит от дублей.
                ts = str(batch["ts"])
                with service.db.transaction() as conn:
                    conn.execute("INSERT OR REPLACE INTO settings VALUES ('longpoll_ts', ?)", (ts,))
            except (TransportError, VkError) as error:
                logger.warning("Связь с VK временно недоступна: %s", type(error).__name__)
                stop.wait(5)
            except Exception as error:
                logger.error("Пакет событий будет обработан повторно: %s", type(error).__name__)
                stop.wait(5)
    except KeyboardInterrupt:
        logger.info("Остановка бота. Очередь и результаты сохранены.")
    finally:
        stop.set()
        thread.join(timeout=25)
