import os
import sys
import json
import re
from datetime import datetime, timezone
from pathlib import Path # <<< ADDED

def parse_issue_body(body):
    """解析 Issue body，提取表单数据"""
    data = {}
    key_map = {
        "补丁名称": "patchName",
        "info.json 内容": "infoJsonContent",
        "translation-manifest.json 内容": "manifestJsonContent"
    }
    sections = re.split(r'###\s+', body)
    for section in sections:
        if not section.strip():
            continue
        lines = section.split('\n', 1)
        key = lines[0].strip()
        value = lines[1].strip().strip('`').strip()
        if value.startswith('json'):
            value = value[4:].strip()
        if key in key_map:
            data[key_map[key]] = value
    return data

def create_slug(text):
    """将文本转换为小写、连字符分隔的 slug"""
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[\s_]+', '-', text).strip().lower()

def main():
    issue_body = os.environ.get('ISSUE_BODY', '')
    issue_author = os.environ.get('ISSUE_AUTHOR', 'unknown-author')
    
    data = parse_issue_body(issue_body)
    if not all(k in data for k in ['patchName', 'infoJsonContent', 'manifestJsonContent']):
        print("::error::Issue form is incomplete.")
        sys.exit(1)

    try:
        info_data = json.loads(data['infoJsonContent'])
        manifest_data = json.loads(data['manifestJsonContent'])
    except json.JSONDecodeError as e:
        print(f"::error::Invalid JSON format provided. Details: {e}")
        sys.exit(1)

    author_slug = issue_author.lower()
    patch_slug = create_slug(data['patchName'])
    patch_dir = Path('patches') / author_slug / patch_slug # <<< CHANGED
    patch_dir.mkdir(parents=True, exist_ok=True)

    info_path = patch_dir / 'info.json' # <<< CHANGED
    manifest_path = patch_dir / 'translation-manifest.json' # <<< CHANGED
    
    with open(info_path, 'w', encoding='utf-8') as f:
        json.dump(info_data, f, indent=4, ensure_ascii=False)
    print(f"Wrote info.json to: {info_path}")

    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest_data, f, indent=4, ensure_ascii=False)
    print(f"Wrote translation-manifest.json to: {manifest_path}")

    index_path = Path('index.json') # <<< CHANGED
    if index_path.exists():
        with open(index_path, 'r', encoding='utf-8') as f: index_data = json.load(f)
    else:
        index_data = {"formatVersion": 1, "lastUpdated": "", "patches": {}}
    
    index_data['lastUpdated'] = datetime.now(timezone.utc).isoformat()
    
    patch_id = info_data.get('patchId')
    if not patch_id:
        print("::error::'patchId' is missing in the provided info.json.")
        sys.exit(1)
        
    latest_version_info = info_data.get('versions', [{}])[0]
    supported_modpacks = latest_version_info.get('supportedModpackVersions', [])

    if not supported_modpacks:
        print("::warning::No 'supportedModpackVersions' found in the latest version entry of info.json.")

    for modpack in supported_modpacks:
        modpack_key = f"{modpack.get('type','').lower()}:{modpack.get('name','')}"
        if not modpack_key or ':' in modpack_key == 1: continue

        if modpack_key not in index_data['patches']:
            index_data['patches'][modpack_key] = []
        
        summary_found = False
        for summary in index_data['patches'][modpack_key]:
            if summary['patchId'] == patch_id:
                summary['latestVersion'] = latest_version_info.get('patchVersion', 'N/A')
                summary['author'] = info_data.get('author', 'N/A')
                summary['description'] = info_data.get('description', '')
                summary_found = True
                break
        
        if not summary_found:
            index_data['patches'][modpack_key].append({
                # <<< FIXED LINE BELOW
                "infoPath": f"./{patch_dir.as_posix()}/info.json",
                "patchId": patch_id, "patchName": info_data.get('patchName', data['patchName']),
                "author": info_data.get('author', 'N/A'), "description": info_data.get('description', ''),
                "latestVersion": latest_version_info.get('patchVersion', 'N/A'), "translationType": "manual",
                "availableDownloadTypes": ["direct"]
            })

    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index_data, f, indent=2, ensure_ascii=False)
    print("Updated index.json")

    pr_title = f"meta: Update metadata for {data['patchName']}"
    pr_body = f"Updates metadata for '{data['patchName']}' as submitted by @{issue_author}.\n\nThis is an automated submission via the advanced metadata template.\n\nCloses #{os.environ.get('ISSUE_NUMBER')}"
    branch_name = f"meta/{author_slug}/{patch_slug}-{datetime.now().strftime('%Y%m%d%H%M')}"
    
    with open(os.environ['GITHUB_OUTPUT'], 'a') as gh_output:
        print(f"branch_name={branch_name}", file=gh_output)
        print(f"pr_title={pr_title}", file=gh_output)
        print(f"pr_body={pr_body}", file=gh_output)

if __name__ == "__main__":
    main()