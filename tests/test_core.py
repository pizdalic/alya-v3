import asyncio
import unittest

from alya.core.container import Container
from alya.core.errors import ContainerError
from alya.core.events import Event, EventBus
from alya.core.lifecycle import Lifecycle


class Closable:
    def __init__(self, log: list[str], name: str) -> None:
        self.log, self.name = log, name

    async def aclose(self) -> None:
        self.log.append(self.name)


class A(Closable):
    pass


class B(Closable):
    pass


class ContainerTests(unittest.IsolatedAsyncioTestCase):
    async def test_singleton_and_reverse_close(self) -> None:
        log: list[str] = []
        c = Container()
        c.register(A, lambda c: A(log, "a"))
        c.register(B, lambda c: (c.resolve(A), B(log, "b"))[1])
        b = c.resolve(B)
        self.assertIs(b, c.resolve(B))
        await c.aclose()
        self.assertEqual(log, ["b", "a"])

    async def test_errors(self) -> None:
        c = Container()
        with self.assertRaises(ContainerError):
            c.resolve(A)
        c.register(A, lambda c: c.resolve(B))  # type: ignore[arg-type,return-value]
        c.register(B, lambda c: c.resolve(A))  # type: ignore[arg-type,return-value]
        with self.assertRaises(ContainerError) as ctx:
            c.resolve(A)
        self.assertIn("циклическая", str(ctx.exception))
        with self.assertRaises(ContainerError):
            c.register(A, lambda c: A([], "x"))

    async def test_close_errors_do_not_stop_others(self) -> None:
        log: list[str] = []

        class Bad:
            def close(self) -> None:
                raise RuntimeError("boom")

        c = Container()
        c.register(A, lambda c: A(log, "a"))
        c.register(Bad, lambda c: Bad())
        c.resolve(A)
        c.resolve(Bad)
        with self.assertLogs("alya.core.container", "ERROR"):
            await c.aclose()
        self.assertEqual(log, ["a"])


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_failing_handler_isolated(self) -> None:
        bus = EventBus()
        got: list[str] = []

        async def ok(e: Event) -> None:
            got.append(e.name)

        async def bad(e: Event) -> None:
            raise ValueError("secret payload must not be logged")

        bus.subscribe("x", bad)
        bus.subscribe("x", ok)
        bus.subscribe("*", ok)
        with self.assertLogs("alya.core.events", "ERROR"):
            res = await bus.publish(Event("x", {"text": "личное"}))
        self.assertEqual((res.delivered, res.failed), (2, 1))
        self.assertEqual(got, ["x", "x"])

    async def test_timeout_and_unsubscribe(self) -> None:
        bus = EventBus(handler_timeout_s=0.05)

        async def slow(e: Event) -> None:
            await asyncio.sleep(1)

        unsub = bus.subscribe("y", slow)
        with self.assertLogs("alya.core.events", "ERROR"):
            res = await bus.publish(Event("y"))
        self.assertEqual(res.failed, 1)
        unsub()
        self.assertEqual((await bus.publish(Event("y"))).delivered, 0)

    async def test_nowait_and_close(self) -> None:
        bus = EventBus()
        got: list[int] = []

        async def h(e: Event) -> None:
            await asyncio.sleep(0.01)
            got.append(1)

        bus.subscribe("z", h)
        bus.publish_nowait(Event("z"))
        bus.publish_nowait(Event("z"))
        await bus.aclose()
        self.assertEqual(got, [1, 1])
        with self.assertRaises(RuntimeError):
            await bus.publish(Event("z"))


class Comp:
    def __init__(self, name: str, log: list[str], fail: bool = False) -> None:
        self.name, self.log, self.fail = name, log, fail

    async def start(self) -> None:
        if self.fail:
            raise RuntimeError("start failed")
        self.log.append(f"start:{self.name}")

    async def stop(self) -> None:
        self.log.append(f"stop:{self.name}")


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_order(self) -> None:
        log: list[str] = []
        lc = Lifecycle()
        lc.add(Comp("a", log))
        lc.add(Comp("b", log))
        await lc.start()
        await lc.stop()
        self.assertEqual(log, ["start:a", "start:b", "stop:b", "stop:a"])

    async def test_rollback_on_failed_start(self) -> None:
        log: list[str] = []
        lc = Lifecycle()
        lc.add(Comp("a", log))
        lc.add(Comp("b", log, fail=True))
        with self.assertRaises(RuntimeError), self.assertLogs("alya.core.lifecycle", "ERROR"):
            await lc.start()
        self.assertEqual(log, ["start:a", "stop:a"])

    async def test_run_until_stopped(self) -> None:
        log: list[str] = []
        lc = Lifecycle()
        lc.add(Comp("a", log))
        task = asyncio.create_task(lc.run_until_stopped(install_signals=False))
        await asyncio.sleep(0.01)
        self.assertEqual(lc.started, ["a"])
        lc.request_stop()
        await asyncio.wait_for(task, 1)
        self.assertEqual(log, ["start:a", "stop:a"])


if __name__ == "__main__":
    unittest.main()
