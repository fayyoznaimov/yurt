"""Файл-замок sync_state.Lock: живой замок не снимается никогда; мёртвый — снимается, в том числе когда pid
из замка достался другому процессу (после перезагрузки: другой boot_id или другое время старта процесса).

    python -m unittest tests.test_sync_state
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sync_state  # noqa: E402


def other_kind(stamp: str) -> str:
    """Метка времени старта «тем же способом», но другая (как у процесса, получившего тот же pid)."""
    kind, _, val = stamp.partition(":")
    return f"{kind}:{int(float(val or 0)) + 12345}"


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "run.lock"
        self.me = os.getpid()
        self.my_start = sync_state.process_start(self.me)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, age_s: float = 0, **data) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")
        if age_s:
            t = time.time() - age_s
            os.utime(self.path, (t, t))

    def lock(self, max_age_s: float = 3600) -> sync_state.Lock:
        return sync_state.Lock(self.path, "test", max_age_s=max_age_s)

    # --- что пишется в замок

    def test_lock_records_boot_id_and_start_time(self):
        lk = self.lock()
        self.assertTrue(lk.try_acquire())
        info = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(info["pid"], self.me)
        self.assertEqual(info["what"], "test")
        self.assertEqual(info["boot_id"], sync_state.boot_id())
        self.assertEqual(info["pid_start"], self.my_start)
        if os.name == "nt" or Path("/proc/self/stat").exists():
            self.assertTrue(self.my_start)                         # на Windows и Linux время старта известно
            self.assertEqual(sync_state.process_start(self.me), self.my_start)   # и не меняется
        if Path("/proc/sys/kernel/random/boot_id").exists():
            self.assertTrue(info["boot_id"])
        self.assertFalse(self.lock().try_acquire())                 # второй запуск — занято
        lk.release()
        self.assertFalse(self.path.exists())

    # --- живой замок

    def test_live_lock_is_never_stolen(self):
        self.write(pid=self.me, boot_id=sync_state.boot_id(), pid_start=self.my_start, what="run.py")
        self.assertFalse(self.lock().try_acquire())
        self.assertTrue(self.path.exists())
        self.write(age_s=7200, pid=self.me, boot_id=sync_state.boot_id(), pid_start=self.my_start, what="run.py")
        with self.assertRaises(sync_state.LockStuck) as cm:                 # старше max_age — выход с подсказкой
            self.lock(max_age_s=3600).try_acquire()
        self.assertIn(str(self.me), str(cm.exception))
        self.assertTrue(self.path.exists())                                 # но замок не тронут

    def test_legacy_lock_without_ids_is_live_while_pid_alive(self):
        self.write(pid=self.me, started="01.10.2026 10:00:00", what="run.py")   # замок старой версии
        self.assertFalse(self.lock().try_acquire())
        self.assertTrue(self.path.exists())

    def test_live_child_process_then_dead(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            start = ""
            for _ in range(50):                                             # процесс мог ещё не запуститься
                start = sync_state.process_start(child.pid)
                if start or not (os.name == "nt" or Path("/proc/self/stat").exists()):
                    break
                time.sleep(0.05)
            self.write(pid=child.pid, boot_id=sync_state.boot_id(), pid_start=start, what="channel.py")
            self.assertFalse(self.lock().try_acquire())                    # жив — занято
        finally:
            child.kill()
            child.wait(10)
        self.assertTrue(self.lock().try_acquire())                          # умер — замок снят и взят

    # --- мёртвый замок

    def test_dead_pid_lock_removed(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait(10)
        self.write(pid=p.pid, boot_id=sync_state.boot_id(), pid_start="x:1", what="run.py")
        self.assertTrue(self.lock().try_acquire())
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["pid"], self.me)

    def test_reused_pid_with_other_start_time_is_dead(self):
        if not self.my_start:
            self.skipTest("время старта процесса на этой системе не узнать")
        # pid из замка жив (это мы), но процесс другой: время старта не совпадает — замок мёртвый
        self.write(age_s=10 * 3600, pid=self.me, boot_id=sync_state.boot_id(), pid_start=other_kind(self.my_start))
        self.assertTrue(self.lock().try_acquire())                          # ни «занято», ни LockStuck
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["pid_start"], self.my_start)

    def test_start_time_of_other_kind_is_not_compared(self):
        if not self.my_start:
            self.skipTest("время старта процесса на этой системе не узнать")
        # метка снята другим способом (например, psutil против /proc) — сравнить нельзя, считаем живым
        self.write(pid=self.me, boot_id=sync_state.boot_id(), pid_start="other:1")
        self.assertFalse(self.lock().try_acquire())

    def test_unknown_start_time_now_is_live(self):
        # нет доступа к процессу (process_start пустой) — не рискуем, замок живой
        self.write(pid=self.me, boot_id=sync_state.boot_id(), pid_start="proc:1")
        with mock.patch.object(sync_state, "process_start", return_value=""):
            self.assertFalse(self.lock().try_acquire())

    def test_boot_change_makes_lock_dead(self):
        # система перезагрузилась: pid из замка жив (это мы), время старта даже «совпало», но boot_id другой
        self.write(age_s=10 * 3600, pid=self.me, boot_id="old-boot-0000", pid_start=self.my_start, what="run.py")
        with mock.patch.object(sync_state, "boot_id", return_value="new-boot-1111"):
            self.assertTrue(self.lock().try_acquire())
            self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["boot_id"], "new-boot-1111")

    def test_same_boot_id_keeps_lock(self):
        self.write(pid=self.me, boot_id="boot-A", pid_start=self.my_start)
        with mock.patch.object(sync_state, "boot_id", return_value="boot-A"):
            self.assertFalse(self.lock().try_acquire())
        with mock.patch.object(sync_state, "boot_id", return_value=""):    # boot_id сейчас не узнать — не сравниваем
            self.assertFalse(self.lock().try_acquire())

    def test_fresh_file_without_pid_is_busy(self):
        self.path.write_text("", encoding="utf-8")                          # другой запуск как раз пишет замок
        self.assertFalse(self.lock().try_acquire())
        t = time.time() - 120
        os.utime(self.path, (t, t))
        self.assertTrue(self.lock().try_acquire())                          # пустой и старый — мусор

    def test_acquire_waits_and_release_only_own(self):
        self.write(pid=self.me, boot_id=sync_state.boot_id(), pid_start=self.my_start)
        said = []
        self.assertFalse(self.lock().acquire(wait_s=0.2, every_s=0.05, say=said.append))
        self.assertTrue(said and "Уже идёт другой запуск" in said[0])
        lk = self.lock()
        lk.held = True
        self.write(pid=self.me + 1, boot_id="", pid_start="")               # чужой замок не удаляем
        lk.release()
        self.assertTrue(self.path.exists())


class HelpersTest(unittest.TestCase):
    def test_stat_starttime(self):
        line = "1234 (my (we) ird) proc) S 1 1234 1234 0 -1 4194560 100 0 0 0 1 2 0 0 20 0 1 0 987654 12345 300"
        self.assertEqual(sync_state.stat_starttime(line), "987654")         # имя со скобками и пробелами
        self.assertEqual(sync_state.stat_starttime("garbage"), "")
        self.assertEqual(sync_state.stat_starttime("1 (x) S 1 2"), "")

    def test_process_start(self):
        self.assertEqual(sync_state.process_start(0), "")
        self.assertEqual(sync_state.process_start(-5), "")
        self.assertEqual(sync_state.process_start("abc"), "")
        self.assertEqual(sync_state.process_start(2 ** 22 + 7), "")         # такого pid нет

    def test_linux_proc_branch(self):
        """Ветка Linux на подставной /proc: boot_id, поле 22 stat, умерший процесс, перезагрузка."""
        with tempfile.TemporaryDirectory() as d:
            proc = Path(d)
            (proc / "self").mkdir()
            (proc / "self" / "stat").write_text("1 (python) S", encoding="utf-8")
            (proc / "sys" / "kernel" / "random").mkdir(parents=True)
            (proc / "sys" / "kernel" / "random" / "boot_id").write_text("b0-0t\n", encoding="ascii")
            me = os.getpid()
            (proc / str(me)).mkdir()
            stat = f"{me} (py thon) S 1 1 1 0 -1 0 0 0 0 0 1 2 0 0 20 0 1 0 555666 1 1"
            (proc / str(me) / "stat").write_text(stat, encoding="utf-8")
            with mock.patch.object(sync_state, "PROC_DIR", proc), \
                    mock.patch.object(sync_state, "BOOT_ID_PATH", proc / "sys" / "kernel" / "random" / "boot_id"):
                self.assertEqual(sync_state.boot_id(), "b0-0t")
                self.assertEqual(sync_state.process_start(me), "proc:555666")
                self.assertEqual(sync_state.process_start(me + 1), "")       # нет /proc/<pid> — пусто, без psutil
                lock_path = proc / "run.lock"
                lk = sync_state.Lock(lock_path, "t")
                self.assertTrue(lk.try_acquire())
                info = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual((info["boot_id"], info["pid_start"]), ("b0-0t", "proc:555666"))
                self.assertFalse(sync_state.Lock(lock_path, "t").try_acquire())   # живой — занято
                # тот же pid, но процесс перезапущен (другое поле 22) — замок прежнего мёртвый
                (proc / str(me) / "stat").write_text(stat.replace("555666", "777888"), encoding="utf-8")
                self.assertTrue(sync_state.Lock(lock_path, "t").try_acquire())
                # перезагрузка: boot_id другой — мёртвый, даже если время старта совпало
                (proc / "sys" / "kernel" / "random" / "boot_id").write_text("new-boot", encoding="ascii")
                self.assertTrue(sync_state.Lock(lock_path, "t").try_acquire())

    def test_boot_id_missing(self):
        with mock.patch.object(sync_state, "BOOT_ID_PATH", Path(tempfile.gettempdir()) / "no-such-boot-id"):
            self.assertEqual(sync_state.boot_id(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
