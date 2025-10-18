import os
import sys
import json
import re
import hashlib
import zipfile
from datetime import datetime, timezone
import requests
import yaml
import shutil

# --- Helper Functions ---

def parse_issue_body(body):
    """解析 Issue Body，将其转换为字典。"""
    data = {}
    # 使用 YAML 解析器来处理 Issue Form 的输出，更稳定
    try:
        # Issue Form 的 body 是类似 markdown 的，但可以被 YAML 解析器粗略处理
        # 移除代码块标记
        body = body.replace('```', '')
        lines = body.splitlines()
        form_data = {}
        current_key = None
        for line in lines:
            if line.startswith('### '):
                current_key = line.replace('### ', '').strip()
                # 将 Issue Form 的 label 转换为 ID
                # 这部分需要根据你的 YML 文件手动映射
                key_map = {
                    "汉化补丁名称": "patchName",
                    "汉化补丁版本号": "patchVersion",
                    "汉化作者/团队名称": "patchAuthor",
                    "补丁描述": "description",
                    "翻译类型": "translationType",
                    "支持的整合包列表": "supportedModpacks",
                    "更新日志": "changelog",
                    "项目网站 (可选)": "website",
                    "安装后提示 (可选)": "postInstallNotes"
                }
                form_data[key_map.get(current_key, current_key)] = []
            elif current_key and line.strip() and line.strip() != '_No response_':
                 form_data[key_map.get(current_key, current_key)].append(line.strip())

        for key, value in form_data.items():
             data[key] = '\n'.join(value)

    except Exception as e:
        print(f"::error::Failed to parse issue body: {e}")
        sys.exit(1)
        
    # 特殊处理支持的整合包
    modpacks_raw = data.get("supportedModpacks", "").strip().splitlines()
    data["supportedModpacksList"] = []
    for line in modpacks_raw:
        if not line: continue
        try:
            type, name, version = [item.strip() for item in line.split(',')]
            data["supportedModpacksList"].append({"type": type, "name": name, "version": version})
        except ValueError:
            print(f"::error::Invalid supportedModpacks format: {line}")
            sys.exit(1)
            
    return data


def find_and_download_zip(issue_number, token):
    """从 Issue 评论中查找 .zip 附件并下载。"""
    headers = {'Authorization': f'token {token}'}
    url = f"https://api.github.com/repos/{os.getenv('REPO_OWNER')}/{os.getenv('REPO_NAME')}/issues/{issue_number}/comments"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    comments = response.json()
    
    # 也要检查 Issue body 本身
    issue_url = f"https://api.github.com/repos/{os.getenv('REPO_OWNER')}/{os.getenv('REPO_NAME')}/issues/{issue_number}"
    issue_response = requests.get(issue_url, headers=headers)
    issue_response.raise_for_status()
    issue_body_text = issue_response.json().get('body', '')

    zip_url_match = re.search(r'https://github\.com/[^/]+/[^/]+/assets/[^\s)]+\.zip', issue_body_text)

    if not zip_url_match:
        print("::error::No .zip attachment found in the issue body.")
        sys.exit(1)
    
    zip_url = zip_url_match.group(0)
    print(f"Found zip attachment: {zip_url}")

    zip_content = requests.get(zip_url, stream=True)
    zip_content.raise_for_status()
    
    with open("patch.zip", "wb") as f:
        for chunk in zip_content.iter_content(chunk_size=8192):
            f.write(chunk)
            
    return "patch.zip"

def generate_file_manifest(zip_path):
    """解压 zip 并为 overrides 内的文件生成清单。"""
    manifest = []
    extract_path = "temp_patch"
    
    if os.path.exists(extract_path):
        shutil.rmtree(extract_path)
    os.makedirs(extract_path)

    with zipfile.ZipFile(zip_path, 'r') as zf:
        # 验证zip结构
        root_folders = {os.path.normpath(f).split(os.sep)[0] for f in zf.namelist()}
        if root_folders != {'overrides'}:
             print(f"::error::ZIP file must only contain an 'overrides' directory at its root. Found: {root_folders}")
             sys.exit(1)
        zf.extractall(extract_path)

    overrides_path = os.path.join(extract_path, "overrides")
    for root, _, files in os.walk(overrides_path):
        for file in files:
            full_path = os.path.join(root, file)
            relative_path = os.path.relpath(full_path, overrides_path).replace('\\', '/')
            
            with open(full_path, 'rb') as f:
                sha1 = hashlib.sha1(f.read()).hexdigest()
            
            manifest.append({
                "operation": "overwrite",
                "path": relative_path,
                "targetPath": relative_path,
                "patchedSha1": sha1
            })
    return manifest

def slugify(text):
    """将文本转换为安全的 slug 格式。"""
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[\s-]+', '-', text)

def set_output(name, value):
    """设置 GitHub Actions 的输出变量。"""
    with open(os.environ['GITHUB_OUTPUT'], 'a') as fh:
        print(f'{name}={value}', file=fh)

# --- Main Logic ---

def main():
    # 1. 获取环境变量和参数
    issue_body = os.getenv("ISSUE_BODY")
    issue_number = os.getenv("ISSUE_NUMBER")
    token = os.getenv("GITHUB_TOKEN")
    repo_full_name = f"{os.getenv('REPO_OWNER')}/{os.getenv('REPO_NAME')}"
    
    if not all([issue_body, issue_number, token]):
        print("::error::Missing one or more required environment variables.")
        sys.exit(1)

    # 2. 解析 Issue
    print("--- Parsing Issue Body ---")
    data = parse_issue_body(issue_body)
    print("Parsed data:", json.dumps(data, indent=2, ensure_ascii=False))

    # 3. 下载并处理附件
    print("\n--- Processing Attachment ---")
    zip_path = find_and_download_zip(issue_number, token)
    file_manifest = generate_file_manifest(zip_path)
    print(f"Generated file manifest for {len(file_manifest)} files.")

    # 4. 准备元数据
    author_slug = slugify(data["patchAuthor"])
    patch_slug = slugify(data["patchName"])
    patch_id = f"{author_slug}/{patch_slug}"
    
    # 5. 生成 translation-manifest.json (内存中)
    # 这一步主要是为了完整性，但我们并不需要把这个文件提交到仓库
    translation_manifest = {
        "formatVersion": 1,
        "patchName": data["patchName"],
        "patchVersion": data["patchVersion"],
        "patchAuthor": data["patchAuthor"],
        "description": data["description"],
        "website": data.get("website", ""),
        "updateInfoUrl": f"https://raw.githubusercontent.com/{repo_full_name}/main/patches/{patch_id}/info.json",
        "translationType": data["translationType"],
        "postInstallNotes": data.get("postInstallNotes", ""),
        "supportedModpacks": data["supportedModpacksList"],
        "fileManifest": file_manifest,
    }
    # print("\n--- Generated translation-manifest.json ---")
    # print(json.dumps(translation_manifest, indent=2, ensure_ascii=False))

    # 6. 更新或创建 info.json
    print("\n--- Updating info.json ---")
    info_path = os.path.join("patches", author_slug, patch_slug, "info.json")
    os.makedirs(os.path.dirname(info_path), exist_ok=True)
    
    if os.path.exists(info_path):
        with open(info_path, 'r', encoding='utf-8') as f:
            info_data = json.load(f)
    else:
        info_data = {
            "formatVersion": 1,
            "patchId": patch_id,
            "patchName": data["patchName"],
            "author": data["patchAuthor"],
            "description": data["description"],
            "website": data.get("website", ""),
            "versions": []
        }

    # 创建新版本条目
    new_version_entry = {
        "patchVersion": data["patchVersion"],
        "releaseDate": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "changelog": data["changelog"],
        "supportedModpackVersions": data["supportedModpacksList"],
        "downloads": [
            {
                "type": "direct",
                "name": "GitHub Release (推荐)",
                # 注意：这里的 URL 是一个预期的格式，需要在PR合并后创建对应的 Release 和 Tag
                "url": f"https://github.com/{repo_full_name}/releases/download/{patch_slug}-{data['patchVersion']}/{patch_slug}-{data['patchVersion']}.htp",
                "sha1": "" # SHA1 可以在 HTP 生成后计算并填入，暂时留空
            }
        ]
    }
    
    # 插入新版本并保持降序
    info_data["versions"].insert(0, new_version_entry)
    
    with open(info_path, 'w', encoding='utf-8') as f:
        json.dump(info_data, f, indent=2, ensure_ascii=False)
    print(f"Successfully updated {info_path}")

    # 7. 更新 index.json
    print("\n--- Updating index.json ---")
    index_path = "index.json"
    if os.path.exists(index_path):
        with open(index_path, 'r', encoding='utf-8') as f:
            index_data = json.load(f)
    else:
        index_data = { "formatVersion": 1, "lastUpdated": "", "patches": {} }
        
    index_data["lastUpdated"] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    
    # 为每个支持的整合包更新索引
    for modpack in data["supportedModpacksList"]:
        modpack_key = f"{modpack['type'].lower()}:{modpack['name']}"
        if modpack_key not in index_data["patches"]:
            index_data["patches"][modpack_key] = []
        
        patch_list = index_data["patches"][modpack_key]
        
        # 查找是否已有此补丁的摘要
        summary = next((p for p in patch_list if p.get("patchId") == patch_id), None)
        
        if summary: # 更新现有摘要
            summary["latestVersion"] = data["patchVersion"]
            summary["description"] = data["description"]
        else: # 添加新摘要
            summary = {
                "infoPath": f"./patches/{author_slug}/{patch_slug}/info.json",
                "patchId": patch_id,
                "patchName": data["patchName"],
                "author": data["patchAuthor"],
                "description": data["description"],
                "latestVersion": data["patchVersion"],
                "translationType": data["translationType"],
                "availableDownloadTypes": ["direct"]
            }
            patch_list.append(summary)
            
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index_data, f, indent=2, ensure_ascii=False)
    print(f"Successfully updated {index_path}")

    # 8. 设置输出给后续步骤使用
    set_output("patch_id", patch_id)
    set_output("patch_name", data["patchName"])
    set_output("patch_version", data["patchVersion"])
    set_output("patch_author", data["patchAuthor"])

    print("\n--- All tasks completed successfully! ---")


if __name__ == "__main__":
    main()