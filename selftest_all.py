# -*- coding: utf-8 -*-
"""一次性跑完所有模块自检：`python3 selftest_all.py`

需要仓库里的 `templates/` 与同级目录的 `_templates/`（部分模块会跳过而不是失败）。
"""
import importlib
import sys

MODULES = ["au_state", "project", "mixer", "chord", "meterkey", "tempo",
           "markers", "automation", "classinfo", "waves", "midi"]


def main():
    failed = []
    for name in MODULES:
        mod = importlib.import_module("logicfmt." + name)
        fn = (getattr(mod, "_selftest", None) or getattr(mod, "selftest", None)
              or getattr(mod, "cmd_selftest", None))
        if fn is None:
            print("%-12s no _selftest" % name)
            continue
        try:
            r = fn() if fn.__code__.co_argcount == 0 else fn(None)
            if r is False:
                failed.append((name, "返回 False"))
                print("%-12s FAIL 返回 False" % name)
            else:
                print("%-12s ok" % name)
        except AssertionError as e:
            failed.append((name, str(e)))
            print("%-12s FAIL %s" % (name, e))
        except Exception as e:                      # noqa: BLE001
            failed.append((name, repr(e)))
            print("%-12s ERROR %r" % (name, e))
    print()
    if failed:
        print("%d module(s) failed" % len(failed))
        return 1
    print("all modules ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
