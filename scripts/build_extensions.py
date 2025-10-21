import os
import argparse
import shutil
import subprocess
import concurrent.futures
import time
from collections import defaultdict
try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # fallback for older Python versions

from utils import progress_bar, run_command_in_dir


def parse_arguments():
    """
    解析命令行参数。

    Returns:
        argparse.Namespace: 包含解析后参数的命名空间对象
    """
    parser = argparse.ArgumentParser(
        description="Build extended versions from extensions.toml files."
    )
    parser.add_argument(
        "extensions",
        type=str,
        nargs="+",
        help="Path(s) to extensions.toml file(s)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="build",
        help="Output directory (default: build)",
    )
    parser.add_argument(
        "-c",
        "--currency",
        type=int,
        default=4,
        help="Number of concurrent operations (default: 4)",
    )
    parser.add_argument(
        "-v",
        "--versions-dir",
        type=str,
        default="versions",
        help="Source versions directory (default: versions)",
    )
    return parser.parse_args()


def parse_extensions_toml(toml_paths):
    """
    解析一个或多个 extensions.toml 文件并合并配置。

    Args:
        toml_paths (list): TOML 文件路径列表

    Returns:
        list: 解析后的扩展配置列表，每个元素包含 metadata 和 mods
    """
    extensions = []

    for toml_path in toml_paths:
        if not os.path.isfile(toml_path):
            print(f"⚠️  Warning: {toml_path} is not a valid file, skipping...")
            continue

        try:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)

            extension = {
                "metadata": data.get("extensions", {}),
                "mods": data.get("mod", []),
                "source_file": toml_path,
            }
            extensions.append(extension)
            print(f"✅ Loaded extension: {extension['metadata'].get('name', 'Unnamed')}")
        except Exception as e:
            print(f"❌ Error parsing {toml_path}: {e}")
            continue

    return extensions


def merge_extension_name(extensions):
    """
    合并多个扩展的名称。

    Args:
        extensions (list): 扩展配置列表

    Returns:
        str: 合并后的扩展名称，使用连字符连接
    """
    names = []
    for ext in extensions:
        name = ext["metadata"].get("name", "unnamed")
        # 转换为小写并替换空格为连字符
        name = name.lower().replace(" ", "-")
        names.append(name)
    return "-".join(names)


def merge_mods(extensions):
    """
    合并多个扩展的 mods 配置。

    使用 (mod_name, loader, version) 作为唯一键，
    按命令行参数顺序，后面的扩展覆盖前面的。

    Args:
        extensions (list): 扩展配置列表

    Returns:
        list: 合并后的 mods 列表
    """
    # 使用字典存储 mods，键为 (mod_name, loader, version)
    mods_dict = {}

    # 按顺序处理每个扩展
    for extension in extensions:
        for mod in extension["mods"]:
            mod_name = mod.get("name", "Unnamed")

            # 处理每个 loader
            for loader in ["fabric", "forge", "neoforge"]:
                if loader not in mod:
                    continue

                loader_config = mod[loader]
                for version, url in loader_config.items():
                    # 使用 (mod_name, loader, version) 作为键
                    key = (mod_name, loader, version)

                    # 如果已存在，后面的覆盖前面的
                    if key in mods_dict:
                        # 更新 URL
                        mods_dict[key]["url"] = url
                    else:
                        # 新增条目
                        mods_dict[key] = {
                            "name": mod_name,
                            "loader": loader,
                            "version": version,
                            "url": url,
                        }

    # 转换回 mod 列表格式
    # 需要重新组织成原来的结构：[[mod], [mod], ...]
    result_mods = {}  # {mod_name: {loader: {version: url}}}

    for (mod_name, loader, version), mod_info in mods_dict.items():
        if mod_name not in result_mods:
            result_mods[mod_name] = {"name": mod_name}

        if loader not in result_mods[mod_name]:
            result_mods[mod_name][loader] = {}

        result_mods[mod_name][loader][version] = mod_info["url"]

    # 转换为列表
    return list(result_mods.values())


def detect_url_platform(url):
    """
    检测 URL 是来自 Modrinth 还是 CurseForge。

    Args:
        url (str): mod 的 URL

    Returns:
        str: 'modrinth' 或 'curseforge'
    """
    if "modrinth.com" in url.lower():
        return "modrinth"
    elif "curseforge.com" in url.lower():
        return "curseforge"
    else:
        # 默认使用 modrinth
        return "modrinth"


def copy_versions(source_dir, output_dir, extension_name):
    """
    复制 versions 目录到输出目录。

    Args:
        source_dir (str): 源 versions 目录路径
        output_dir (str): 输出目录路径
        extension_name (str): 扩展名称

    Returns:
        str: 复制后的目录路径
    """
    target_dir = os.path.join(output_dir, extension_name)

    if os.path.exists(target_dir):
        print(f"🗑️  Removing existing directory: {target_dir}")
        shutil.rmtree(target_dir)

    print(f"📁 Copying {source_dir} to {target_dir}...")
    shutil.copytree(source_dir, target_dir)
    print(f"✅ Copy completed")

    return target_dir


def overlay_extension_files(extension_toml_path, target_dir):
    """
    覆盖扩展目录中的额外文件到构建目录。

    检查扩展 toml 文件同级目录下是否存在 versions/ 子目录，
    如果存在则将其内容覆盖到目标构建目录中。

    Args:
        extension_toml_path (str): 扩展 toml 文件的路径
        target_dir (str): 目标构建目录路径

    Returns:
        bool: 如果有文件被覆盖返回 True，否则返回 False
    """
    # 获取 toml 文件所在目录
    extension_dir = os.path.dirname(os.path.abspath(extension_toml_path))
    extension_versions_dir = os.path.join(extension_dir, "versions")

    # 检查是否存在 versions 子目录
    if not os.path.isdir(extension_versions_dir):
        return False

    print(f"📋 Overlaying extension files from {extension_versions_dir}...")

    try:
        # 统计要覆盖的文件数
        file_count = 0
        for root, dirs, files in os.walk(extension_versions_dir):
            file_count += len(files)

        if file_count == 0:
            print("⚠️  No files found in extension versions directory")
            return False

        # 使用 copytree 的 dirs_exist_ok=True 参数覆盖文件
        shutil.copytree(extension_versions_dir, target_dir, dirs_exist_ok=True)
        print(f"✅ Overlayed {file_count} file(s) from extension directory")
        return True

    except Exception as e:
        print(f"❌ Error overlaying extension files: {e}")
        return False


def update_pack_versions(base_dir, extension_suffix):
    """
    更新所有版本目录下的 pack.toml，在 version 字段尾部添加扩展名后缀。

    Args:
        base_dir (str): 基础目录
        extension_suffix (str): 要添加的扩展名后缀（如 "opti-utils"）

    Returns:
        int: 更新的文件数量
    """
    updated_count = 0
    suffix = f"-{extension_suffix}"

    print(f"📝 Updating pack.toml versions with suffix '{suffix}'...")

    for loader in os.listdir(base_dir):
        loader_path = os.path.join(base_dir, loader)
        if not os.path.isdir(loader_path):
            continue

        for version in os.listdir(loader_path):
            version_path = os.path.join(loader_path, version)
            if not os.path.isdir(version_path):
                continue

            pack_toml_path = os.path.join(version_path, "pack.toml")
            if not os.path.isfile(pack_toml_path):
                continue

            try:
                # 读取 pack.toml
                with open(pack_toml_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                # 查找并修改 version 行
                modified = False
                for i, line in enumerate(lines):
                    # 匹配 version = "..." 行
                    if line.strip().startswith("version ="):
                        # 提取当前版本号
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            # 提取引号内的版本号
                            value = parts[1].strip()
                            if value.startswith('"') and value.endswith('"'):
                                current_version = value[1:-1]
                                # 在版本号尾部添加后缀
                                new_version = current_version + suffix
                                # 重新构造行
                                lines[i] = f'version = "{new_version}"\n'
                                modified = True
                                break

                # 如果修改了，写回文件
                if modified:
                    with open(pack_toml_path, "w", encoding="utf-8") as f:
                        f.writelines(lines)
                    updated_count += 1

            except Exception as e:
                print(f"⚠️  Warning: Failed to update {pack_toml_path}: {e}")

    if updated_count > 0:
        print(f"✅ Updated {updated_count} pack.toml file(s)")
    else:
        print("⚠️  No pack.toml files were updated")

    return updated_count


def get_version_path(base_dir, loader, version):
    """
    获取特定 loader 和版本的完整路径。

    Args:
        base_dir (str): 基础目录
        loader (str): mod loader (fabric/forge/neoforge)
        version (str): MC 版本号

    Returns:
        str: 完整路径，如果目录不存在则返回 None
    """
    path = os.path.join(base_dir, loader, version)
    return path if os.path.isdir(path) else None


def add_mod_to_version(version_path, mod_url, platform, max_retries=3):
    """
    向指定版本添加单个 mod，失败时自动重试。

    Args:
        version_path (str): 版本目录路径
        mod_url (str): mod 的 URL
        platform (str): 平台 ('modrinth' 或 'curseforge')
        max_retries (int): 最大重试次数，默认 3

    Returns:
        tuple: (success: bool, attempts: int) 成功状态和尝试次数
    """
    if not os.path.isdir(version_path):
        return False, 0

    platform_cmd = "mr" if platform == "modrinth" else "cf"
    command = f"packwiz {platform_cmd} add {mod_url} --yes"

    # 重试延迟（秒）：1, 3, 9
    retry_delays = [1, 3, 9]

    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=version_path,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode == 0:
                return True, attempt + 1  # 成功，返回尝试次数

            # 失败但还有重试次数
            if attempt < max_retries - 1:
                delay = retry_delays[attempt]
                time.sleep(delay)

        except Exception:
            # 异常但还有重试次数
            if attempt < max_retries - 1:
                delay = retry_delays[attempt]
                time.sleep(delay)

    return False, max_retries  # 所有重试都失败


def add_mods_to_versions(base_dir, mods, currency=4):
    """
    批量添加 mods 到对应的版本目录。

    Args:
        base_dir (str): 基础目录
        mods (list): mod 配置列表
        currency (int): 并发数

    Returns:
        dict: 失败的 mods 信息
    """
    # 收集所有需要安装的 mod 任务
    tasks = []
    for mod in mods:
        mod_name = mod.get("name", "Unnamed")
        for loader in ["fabric", "forge", "neoforge"]:
            loader_config = mod.get(loader, {})
            for version, url in loader_config.items():
                version_path = get_version_path(base_dir, loader, version)
                if version_path:
                    platform = detect_url_platform(url)
                    tasks.append(
                        {
                            "name": mod_name,
                            "loader": loader,
                            "version": version,
                            "url": url,
                            "platform": platform,
                            "version_path": version_path,
                        }
                    )

    if not tasks:
        print("⚠️  No mods to install")
        return {}

    print(f"\n📦 Installing {len(tasks)} mod(s) across different versions...\n")

    failed = defaultdict(list)
    completed = 0
    total = len(tasks)

    def install_task(task):
        """执行单个安装任务"""
        success, attempts = add_mod_to_version(
            task["version_path"], task["url"], task["platform"]
        )
        return task, success, attempts

    with concurrent.futures.ThreadPoolExecutor(max_workers=currency) as executor:
        future_to_task = {executor.submit(install_task, task): task for task in tasks}

        for future in concurrent.futures.as_completed(future_to_task):
            task, success, attempts = future.result()
            completed += 1

            # 显示状态和重试信息
            if success:
                retry_info = f" (重试 {attempts - 1} 次)" if attempts > 1 else ""
                status = f"✅{retry_info}"
            else:
                status = f"❌ (失败，已重试 {attempts} 次)"

            print(
                f"{progress_bar(completed, total)} | {task['name']} "
                f"[{task['loader']}/{task['version']}] {status}"
            )

            if not success:
                key = f"{task['loader']}/{task['version']}"
                failed[key].append(task["name"])

    return dict(failed)


def export_modpacks(base_dir):
    """
    导出所有版本的整合包。

    Args:
        base_dir (str): 基础目录

    Returns:
        list: 导出失败的路径列表
    """
    version_paths = []
    for loader in os.listdir(base_dir):
        loader_path = os.path.join(base_dir, loader)
        if not os.path.isdir(loader_path):
            continue
        for version in os.listdir(loader_path):
            version_path = os.path.join(loader_path, version)
            if os.path.isdir(version_path):
                version_paths.append(version_path)

    if not version_paths:
        print("⚠️  No versions found to export")
        return []

    print(f"\n📤 Exporting {len(version_paths)} modpack(s)...\n")

    failed = []
    for idx, path in enumerate(version_paths, 1):
        print(f"{progress_bar(idx - 1, len(version_paths))} | Exporting: {path}")

        try:
            result = subprocess.run(
                "packwiz mr export",
                shell=True,
                cwd=path,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode == 0:
                print(f"{progress_bar(idx, len(version_paths))} | {path} ✅")
            else:
                print(f"{progress_bar(idx, len(version_paths))} | {path} ❌")
                failed.append(path)
        except Exception as e:
            print(f"{progress_bar(idx, len(version_paths))} | {path} ❌ ({e})")
            failed.append(path)

    return failed


def main():
    """主函数"""
    args = parse_arguments()

    # 确保输出目录存在
    os.makedirs(args.output, exist_ok=True)

    # 解析所有 extensions.toml 文件
    print("=" * 60)
    print("📋 Parsing extension configurations...")
    print("=" * 60)
    extensions = parse_extensions_toml(args.extensions)

    if not extensions:
        print("❌ No valid extensions found")
        return

    # 合并扩展名称
    merged_name = merge_extension_name(extensions)

    # 显示扩展信息
    print("\n" + "=" * 60)
    print(f"🔧 Building merged extension: {merged_name}")
    extension_info = ", ".join(
        [
            f"{ext['metadata'].get('name', 'Unnamed')} ({ext['metadata'].get('version', 'N/A')})"
            for ext in extensions
        ]
    )
    print(f"   Extensions: {extension_info}")
    print("=" * 60)

    # 1. 复制基础 versions 目录
    target_dir = copy_versions(args.versions_dir, args.output, merged_name)

    # 2. 按顺序覆盖每个扩展的 versions/ 文件
    for extension in extensions:
        overlay_extension_files(extension["source_file"], target_dir)

    # 3. 合并所有 mods（后面的覆盖前面的）
    merged_mods = merge_mods(extensions)

    # 4. 添加合并后的 mods
    failed_mods = add_mods_to_versions(target_dir, merged_mods, args.currency)

    # 5. 更新 pack.toml 版本号（添加扩展名后缀）
    print()  # 添加空行
    update_pack_versions(target_dir, merged_name)

    # 6. 导出整合包
    failed_exports = export_modpacks(target_dir)

    # 报告结果
    print("\n" + "=" * 60)
    print(f"📊 Summary for {merged_name}")
    print("=" * 60)

    if failed_mods:
        print("\n❌ Failed to install the following mods:")
        for version, mod_names in failed_mods.items():
            print(f"   [{version}]")
            for mod_name in mod_names:
                print(f"     - {mod_name}")

    if failed_exports:
        print("\n❌ Failed to export the following versions:")
        for path in failed_exports:
            print(f"   - {path}")

    if not failed_mods and not failed_exports:
        print("\n✅ All operations completed successfully!")

    print(f"\n📂 Output directory: {target_dir}")


if __name__ == "__main__":
    main()
