# -*- coding: utf-8 -*-
"""命令行测试：直接运行一个项目的步骤，不依赖 PyQt6。
用法：python run_cli.py [项目名]
不传项目名则交互选择。
"""
import sys

from smart_tool.core.project_store import list_projects
from smart_tool.core.step_executor import StepExecutor


def main():
    projects = list_projects()
    if not projects:
        print("没有可用项目。")
        return

    if len(sys.argv) > 1:
        name = sys.argv[1]
    else:
        print("可用项目：")
        for p in projects:
            print(f"  {p.name}")
        name = input("输入项目名：").strip()

    store = next((p for p in projects if p.name == name), None)
    if not store:
        print(f"项目不存在：{name}")
        return

    steps = store.load_steps()
    variables = store.load_variables()
    print(f"加载项目 [{name}]，共 {len(steps)} 步。")
    executor = StepExecutor(
        steps, variables, headless=False, project_dir=store.dir,
    )
    try:
        executor.run()
    except Exception as e:
        print(f"\n执行失败：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
