import sys

from app_paths import configure_current_process


def main() -> int:
    configure_current_process()

    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        from otc_promo_workflow import main as worker_main

        sys.argv = [sys.argv[0], *sys.argv[2:]]
        return int(worker_main())

    from ui_main import YunFengEditorUI

    app = YunFengEditorUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
