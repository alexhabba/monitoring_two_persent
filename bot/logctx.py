"""Имя счёта для строк лога, которые пишутся из потоков копирующего счёта (в т.ч. из рабочих потоков закрытия)."""
import threading

ctx = threading.local()


def set_account(name):
    ctx.name = name
