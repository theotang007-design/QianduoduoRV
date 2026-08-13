"""运行环境兼容补丁。

当前机器的 Python 为 3.10.0b4（beta 版），标准库 types 模块缺少正式版才导出的
UnionType 属性，会导致 typing_extensions / beautifulsoup4 / akshare 导入失败。
此处在不改动第三方库的前提下补齐该属性。

正式发布的 Python 3.10+ 不受影响（hasattr 判断后直接跳过）。
"""
import types

if not hasattr(types, "UnionType"):
    types.UnionType = type(int | str)