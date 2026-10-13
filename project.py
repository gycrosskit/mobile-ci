"""双仓库执行的应用输入；Pipeline 显式传入应用根目录。"""

import importlib.util
import argparse
import os
from pathlib import Path
import re

from project_config import load_project, profile_for_job, ProjectConfigError

TOOL_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = Path(os.environ.get("APP_SOURCE_ROOT", Path.cwd())).resolve()


def selected_project():
    profile = load_project(os.environ.get("CI_PROJECT_ID", "sxmqlive"))
    job = os.environ.get("JOB_NAME")
    if job and profile_for_job(job)["id"] != profile["id"]:
        raise ProjectConfigError("当前 Job 与可信项目配置不匹配")
    return profile


def provider_namespace(provider):
    profile = selected_project()
    binding = profile.get("providers", {}).get(provider)
    if not binding:
        raise ProjectConfigError("项目没有声明供应商凭据绑定")
    return {"project_id": profile["id"], "provider_account_id": binding["accountId"]}, binding.get("legacyNamespace", False)


def app_path(relative, *, exists=False):
    from project_config import relative_path
    relative_path(relative)
    path = SOURCE_ROOT / relative
    if any(part.is_symlink() for part in (path, *path.parents)) or SOURCE_ROOT not in path.resolve().parents:
        raise ProjectConfigError("项目路径必须位于应用根内且不能使用符号链接")
    if exists and not path.exists():
        raise ProjectConfigError("可信项目路径不存在：" + relative)
    return path


def platform_config(platform):
    config = selected_project()["platforms"].get(platform)
    if not config:
        raise ProjectConfigError("项目未声明平台：" + platform)
    return config


def _expand(value, **fields):
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise ProjectConfigError("项目模板必须为单行字符串")
    for key in re.findall(r"\{([^{}]+)\}", value):
        if key not in fields:
            raise ProjectConfigError("项目模板包含未知占位符")
        value = value.replace("{" + key + "}", fields[key])
    if "{" in value or "}" in value:
        raise ProjectConfigError("项目模板格式无效")
    return value


def android_contract(brand, pipeline, engine):
    config = platform_config("android")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", brand) or pipeline not in ("test", "release"):
        raise ProjectConfigError("Android 品牌或流水线无效")
    if engine not in config.get("engines", ["cmp", "kuikly"] if config["adapter"] == "sxmqlive" else ["native"]):
        raise ProjectConfigError("UI 引擎不属于可信项目")
    module = config.get("modules", {}).get(engine)
    build_type = config.get("buildTypes", {}).get(pipeline)
    if not module or not build_type or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", build_type):
        raise ProjectConfigError("Android module/buildType 声明不完整")
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", part) for part in module.split("/")):
        raise ProjectConfigError("Android module 必须为固定项目路径")
    fields = {"brand": brand, "brandPascal": brand[0].upper() + brand[1:], "module": module,
              "buildType": build_type, "buildTypeLower": build_type.lower(), "pipeline": pipeline}
    variant = _expand(config.get("variants", {}).get(pipeline, "{brandPascal}{buildType}" if config["adapter"] == "sxmqlive" else "{buildType}"), **fields)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", variant):
        raise ProjectConfigError("Android variant 声明无效")
    fields["variant"] = variant
    artifact = _expand(config.get("artifact", "{module}/build/outputs/apk/{brand}/{buildTypeLower}/output-metadata.json"), **fields)
    metadata = _expand(config.get("metadata", artifact if artifact.endswith("output-metadata.json") else str(Path(artifact).parent / "output-metadata.json")), **fields)
    mapping = _expand(config.get("mapping", "{module}/build/outputs/mapping/{brand}{buildType}" if config["adapter"] == "sxmqlive" else "{module}/build/outputs/mapping/{variant}"), **fields)
    for relative in (module, artifact, metadata, mapping):
        app_path(relative)
    return {"adapter": config["adapter"], "module": module, "buildType": build_type, "variant": variant,
            "metadata": metadata, "artifact": artifact, "outputDir": str(Path(metadata).parent),
            "mapping": mapping, "applicationId": config.get("applicationId")}


def ios_build_contract(engine):
    config = platform_config("ios")
    if engine not in config.get("engines", ["cmp", "kuikly"] if config["adapter"] == "sxmqlive" else ["native"]):
        raise ProjectConfigError("UI 引擎不属于可信项目")
    legacy = config["adapter"] == "sxmqlive"
    host = config.get("hostDirs", {}).get(engine, "")
    def field(name, default=""):
        value = config.get(name, default)
        return value.get(engine, "") if isinstance(value, dict) else value
    workspace = field("workspace", host + "/" + host + ".xcworkspace" if host else "")
    project = field("project", host + "/" + host + ".xcodeproj" if host else "")
    container = workspace or project
    if not container:
        raise ProjectConfigError("iOS 必须声明 workspace 或 project")
    app_path(container)
    scheme = field("scheme", "MiaoshouKuikly" if engine == "kuikly" else "Miaoshou")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_. -]*", scheme):
        raise ProjectConfigError("iOS scheme 无效")
    configuration = field("configuration", "Release")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_. -]*", configuration):
        raise ProjectConfigError("iOS configuration 无效")
    return {"adapter": config["adapter"], "kind": "-workspace" if workspace else "-project", "container": str(app_path(container)),
            "destinationKind": "-project" if project else "-workspace", "destination": str(app_path(project or workspace)),
            "scheme": scheme, "requireGradle": legacy, "configuration": configuration}


def ios_xcconfig(relative):
    config = platform_config("ios")
    if config["adapter"] == "sxmqlive":
        valid = re.fullmatch(r"iosApp/Configuration/[A-Za-z0-9][A-Za-z0-9._-]*\.xcconfig", relative)
    else:
        declared = config.get("xcconfig", {})
        valid = relative in (declared.values() if isinstance(declared, dict) else [declared])
    if not valid:
        raise ProjectConfigError("xcconfig 不属于可信项目声明")
    return app_path(relative, exists=True)


def harmony_contract(brand):
    config = platform_config("harmony")
    module = config.get("module")
    if not module:
        raise ProjectConfigError("Harmony 必须声明 module")
    app_path(module)
    native = config["adapter"] == "hvigor"
    product = _expand(config.get("product", "{brand}"), brand=brand)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", product):
        raise ProjectConfigError("Harmony product 无效")
    artifact = _expand(config.get("artifact", "{module}/build/{product}/outputs/{product}/entry-{product}-unsigned.hap"), module=module, product=product, brand=brand)
    app_path(artifact)
    if not artifact.endswith(".hap") or Path(artifact).parent == Path(module).parent:
        raise ProjectConfigError("Harmony 必须声明 module 构建目录下的 HAP")
    if Path(module) not in Path(artifact).parents:
        raise ProjectConfigError("Harmony HAP 必须位于 module 内")
    return {"adapter": config["adapter"], "module":module, "product":product, "artifact":artifact,
            "app":str(Path(module).parent), "moduleName":Path(module).name,
            "uiEngine":"native" if native else "kuikly", "metadata":config.get("metadata", "")}


def application_config_module():
    paths = (SOURCE_ROOT / "ci/scripts/verify_ios_project_configuration.py",
             SOURCE_ROOT / "tools/verify_ios_project_configuration.py")
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        raise ValueError("应用根缺少品牌配置检查入口；请设置 APP_SOURCE_ROOT")
    spec = importlib.util.spec_from_file_location("application_brand_config", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description="受信项目构建数据，不执行构建或外部操作")
    parser.add_argument("command", choices=("android", "ios", "xcconfig"))
    parser.add_argument("values", nargs="*")
    args = parser.parse_args()
    if args.command == "android" and len(args.values) == 3:
        value = android_contract(*args.values)
        for key in ("module", "buildType", "variant", "mapping", "adapter"):
            print(value[key])
    elif args.command == "ios" and len(args.values) == 1:
        value = ios_build_contract(args.values[0])
        for key in ("kind", "container", "destinationKind", "destination", "scheme", "configuration", "adapter"):
            print(value[key])
    elif args.command == "xcconfig" and len(args.values) == 1:
        print(ios_xcconfig(args.values[0]))
    else:
        parser.error("构建参数数量无效")


if __name__ == "__main__":
    main()
