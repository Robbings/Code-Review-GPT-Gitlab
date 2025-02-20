import concurrent
import json
import re
from idlelib.configdialog import changes

from review_engine.abstract_handler import ReviewHandle
from utils.gitlab_parser import filter_diff_content, filter_diff_new_line
from utils.logger import log


def find_code_block_close(file_content, start_line, start_word):
    lines = file_content.split('\n')
    open_braces = 0
    in_block_comment = False
    in_line_comment = False
    start_parse = False

    def is_start_of_block_comment(line, index):
        return line[index:index+2] == '/*'

    def is_end_of_block_comment(line, index):
        return line[index:index+2] == '*/'

    def is_start_of_line_comment(line, index):
        return line[index:index+2] == '//'

    for i in range(start_line - 1, len(lines)):
        line = lines[i]
        j = 0
        in_line_comment = False
        while j < len(line):
            if in_block_comment: # 如果在块注释中
                if is_end_of_block_comment(line, j):
                    in_block_comment = False
                    j += 2
                    continue
            elif in_line_comment:
                if line[j] == '\n':
                    in_line_comment = False
            else:
                if is_start_of_block_comment(line, j):
                    in_block_comment = True
                    j += 2
                    continue
                if is_start_of_line_comment(line, j):
                    in_line_comment = True
                    break
                if not start_parse:
                    if line[j:j+len(start_word)] == start_word:
                        j += len(start_word)
                        start_parse = True
                        continue
                else:
                    if line[j] == '{':
                        open_braces += 1
                    elif line[j] == '}':
                        open_braces -= 1
                        if open_braces == 0:
                            return i + 1
            j += 1

    return -1  # 如果找不到完整的代码块，则返回 -1

def find_content_in_file(file_path, pattern, struct_name = "Not stated"):
    if not file_path:
        log.error(f"SyncPoolHandler: 未找到结构体{struct_name}！")
        return
    # 遍历每一个文件
    ret = ""
    file_content = ""
    line_numbers = []
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            for line_number, line in enumerate(file, start=1):
                file_content += line.strip()  + '\n'
                if re.search(pattern, line):
                    line_numbers.append(line_number)
    except FileNotFoundError:
        log.error(f"File not found: {file_path}")
    except IOError as e:
        log.error(f"Error reading file {file_path}: {e}")
    if not file_content:
        log.error(f"SyncPoolHandler: 未找到结构体{struct_name}！")
        return
    for line_number in line_numbers:
        close_num = find_code_block_close(file_content, line_number, struct_name)
        if close_num == -1:
            log.error(f"SyncPoolHandler: 未找到{struct_name}的闭合括号！")
            continue
        # 将file_content按行分割，取出line_number到close_num行的内容
        files = file_content.split('\n')
        ret += '\n'.join(files[line_number - 1:close_num])
    return ret

class SyncPoolHandler(ReviewHandle):

    def __init__(self):
        super().__init__()
        self.maximum_files = 50 # 最大文件数, 超过此文件数则不进行review
        self.white_project_list = [] # 白名单, 在白名单中的项目进行review，为空时所有项目进行review，优先级高于黑名单
        self.black_project_list = [] # 黑名单, 在黑名单中的项目不进行review

    def merge_handle(self, gitlabMergeRequestFetcher, gitlabRepoManager, hook_info, reply, model):
        # 获取项目名称
        project_name = hook_info['project']['name']
        if self.white_project_list and project_name not in self.white_project_list:
            log.info(f"SyncPoolHandler: {project_name}不在白名单中，不进行review！")
            return
        if self.black_project_list and project_name in self.black_project_list:
            log.info(f"SyncPoolHandler: {project_name}在黑名单中，不进行review！")
            return
        changes = gitlabMergeRequestFetcher.get_changes()
        merge_info = gitlabMergeRequestFetcher.get_info()
        self.gitlabMergeRequestFetcher = gitlabMergeRequestFetcher
        self.gitlabRepoManager = gitlabRepoManager
        self.model = model
        self.reply = reply
        source_branch_name = merge_info['source_branch']

        label = 1
        # 生成review_note
        review_note = ""
        flag = False
        for change in changes:
            if change['new_path'].endswith('.go'):
                # 处理diff
                diffs = filter_diff_content(change['diff'])
                # diffs中包含sync.Pool的使用
                if diffs and 'sync.Pool' in diffs:
                    file_content = gitlabMergeRequestFetcher.get_file_content(change['new_path'], source_branch_name)
                    if not file_content:
                        log.error(f"SyncPoolHandler: 获取文件{change['new_path']}内容失败！")
                        continue
                    # 从diff中获取修改的行号
                    line_numbers = filter_diff_new_line(change['diff'])
                    # 遍历line_numbers[0]到line_numbers[1] + 1行，查找sync.Pool的使用
                    content_relation = self.search_sync_pool(file_content, line_numbers[0], line_numbers[1] + 1)
                    if not content_relation:
                        continue
                    for content in content_relation:
                        single_note = self.generate_review_note(content, source_branch_name) + '\n\n'
                        flag = True
                        if '不存在问题' not in single_note:
                            # 正则表达式，在三级标题后添加序号
                            pattern = re.compile(r'^(### )(.*)', re.MULTILINE)
                            # 替换匹配到的三级标题，添加序号
                            def replacer(match):
                                nonlocal label
                                result = f"{match.group(1)}{label}. {match.group(2)}"
                                label += 1
                                return result
                            review_note += pattern.sub(replacer, single_note)
        if flag:
            # 发送review_note
            if '该Reset存在问题' in review_note:
                reply.add_reply({
                    'title': '❗️该Merge存在sync.Pool使用问题',
                    'content': (
                        f"## 项目名称: **{hook_info['project']['name']}**\n\n"
                        f"### 合并请求详情\n"
                        f"- **MR URL**: [查看合并请求]({hook_info['object_attributes']['url']})\n"
                        f"- **源分支**: `{hook_info['object_attributes']['source_branch']}`\n"
                        f"- **目标分支**: `{hook_info['object_attributes']['target_branch']}`\n\n"
                        f"### 变更详情\n"
                        f"- **修改文件个数**: `{len(changes)}`\n"
                        f"- **Code Review 状态**: ❌\n"
                        f"- **详细情况请查看gitlab**\n"
                    ),
                    'target': 'dingtalk',
                    'msg_type': 'SINGLE',
                })
            if label == 1:
                reply.add_reply({
                    'title': '特定问题：sync.Pool使用问题审查报告',
                    'content': '#### ❕请注意：提交代码中出现了sync.Pool语法的使用！\n\n#### 🎉 大模型辅助检测认为本次Merge不存在sync.Pool使用问题\n\n#### ⚠️ 大模型审查结果仅供参考，请仔细检查代码逻辑！',
                    'msg_type': 'SINGLE',
                    'target': 'all',
                })
            else:
                reply.add_reply({
                    'title': '特定问题：sync.Pool使用问题审查报告',
                    'content': review_note,
                    'msg_type': 'SINGLE',
                    'target': 'all',
                })

    def search_sync_pool(self, file_content, start_line, end_line):
        content_relation = []
        files = file_content.split('\n')
        for line_num in range(start_line, end_line):
            line = files[line_num - 1]
            if 'sync.Pool' in line:
                close_num = find_code_block_close(file_content, line_num, 'sync.Pool')
                if close_num == -1:
                    log.info(f"SyncPoolHandler: {line}行处不是完整代码，忽略。")
                    continue
                # content_relation[line_num]等于从line_num到close_num行的内容
                content_relation.append('\n'.join(files[line_num - 1:close_num - 1]))
        return content_relation

    def generate_review_note(self, content, branch_name="main"):
        # 构建提示词模板
        review_note = f"输入给你的代码中使用了sync.Pool，请找到该代码中使用到的结构体的名字。返回格式为一个json字符串，包含两个字段：\n" \
                        f"1. resaon: 解释\n" \
                        f"2. name: 结构体的名字\n" \
                        f"示例：\n" \
                        f"{{\n" \
                        f"    \"reason\": \"你的推理\",\n" \
                        f"    \"name\": \"结构体的名字\"\n" \
                        f"}}\n"\
                        f"请务必注意：只回复和示例格式一致的符合规范的json字符串，不要包含其他任何内容，不需要包装在markdown代码块中，不需要任何文字解释。"
        messages = [
            {"role": "system",
             "content": review_note
             },
            {"role": "user",
             "content": f"输入给你的代码是{content}",
             },
        ]
        has_coded = False
        ret_content = ""
        parsed_dict = {}
        for _ in range(5):
            try:
                self.model.generate_text(messages)
                ret_content = self.model.get_respond_content().strip().strip("```json").strip("```").strip()
                ret_content = ret_content.replace('\n', '')
                parsed_dict = json.loads(ret_content)
                has_coded = True
                break
            except json.JSONDecodeError:
                has_coded = False
        if not has_coded:
            log.error(f"SyncPoolHandler: 无法解析json字符串{ret_content}！")
            return
        struct_name = parsed_dict['name']
        # 正则表达式：
        sys_str = """
         你是一位资深编程专家，请你针对sync.Pool中使用的结构体进行review，根据结构体的具体内容，检查结构体的Reset方法是否将结构体内的所有字段都置为初始化状态(置为空); 避免对象被复用时,内有脏数据。
         你的返回内容必须严格遵守下面的格式，包括标题内容。模板中的变量内容解释：
         变量5为：代码中的优点 变量1有几个选项：❌该Reset存在问题或✅该Reset不存在问题🤔未找到相关代码 ❕可能存在问题。变量2是：code review发现的问题点，如果不存在问题则填写无，如果存在问题请指出。 变量3是：具体的修改建议，如果不存在问题则填写无，如果存在问题请指出。变量4是：你给出的修改后的代码，如果不存在问题则填写无，如果存在问题请指出。 
         必须要求：1. 以精炼的语言、严厉的语气指出存在的问题。2. 你的反馈内容必须使用严谨的markdown格式 3. 不要携带变量内容解释信息。4. 有清晰的标题结构。有清晰的标题结构。有清晰的标题结构。 5. 提供给你的代码并不完整，请只Reset方法是否将结构体内的所有字段都置为初始化状态(置为空)，不用关注代码正确性和格式问题。
         6.当缺少必要信息比如reset的内容或定义结构体的代码时，变量1请返回：🤔未找到相关代码 7.当且仅当存在变量创建后未在Reset中操作或未创建却在Reset中操作时，变量1返回：❌该Reset存在问题。若Reset中操作不当，变量1返回：❕可能存在问题
         8.修改后的代码请使用diff方式，标注清晰删除了哪些代码，添加了哪些代码。 
         9. 检查方法如下： a. 在结构体定义中找到结构体的字段 b. 在Reset方法中找到结构体的Reset方法 c. 检查Reset方法中是否将结构体内的所有字段都置为初始化状态(置为空) d. 重复a-c步骤，直到所有结构体都检查完毕
         检查示例： 结构体存在DspStrategyPathId字段，Reset方法中存在wb.DspStrategyPathId = 0，检查通过；再次检查结构体存在DspStrategyPathId字段，Reset方法中不存在wb.DspStrategyPathId = 0，检查不通过。
         10. 你的返回格式严格如下：



### {struct_name}：

#### 🌟结论：{变量1}

#### 🤔问题点：
{变量2}

#### 🎯修改建议：
{变量3}

#### 💻修改后的代码：
```diff
{变量4}
```

---

```
         """
        sys_str = sys_str.replace("{struct_name}", struct_name)
        user_str = "相关代码片段如下：\n\n```go\n\n"
        # 找到结构体定义
        pattern = rf'\b{re.escape(struct_name)}\b\s+struct\b'
        struct_files = self.gitlabRepoManager.find_files_by_keyword(pattern, branch_name)
        relate_code = ""
        for struct_file in struct_files:
            relate_code += find_content_in_file(struct_file, pattern, struct_name)
        relate_code += '\n\n'
        # 找到结构体reset方法
        pattern = rf'func\s*\(\s*\w+\s*\*\s*{re.escape(struct_name)}\s*\)\s*Reset\s*\(\s*\)'
        struct_files = self.gitlabRepoManager.find_files_by_keyword(pattern, branch_name)
        for struct_file in struct_files:
            relate_code += find_content_in_file(struct_file, pattern, struct_name)
        relate_code += '\n\n'
        user_str += relate_code + '```'
        messages = [
            {"role": "system",
             "content": sys_str
             },
            {"role": "user",
             "content": f"输入给你的代码是{user_str}",
             },
        ]
        log.info(f"发送给gpt 内容如下：{messages}")
        self.model.generate_text(messages)
        ret_msg = self.model.get_respond_content()
        if '未找到相关代码' or '该Reset不存在问题' in ret_msg:
            return ret_msg
        # 重复发送5次，记录不同种类返回的次数
        msg_dict = {
            'success' : [],
            'error' : [],
            'other' : []
        }
        if '该Reset存在问题' in ret_msg:
            msg_dict['error'].append(ret_msg)
        else:
            msg_dict['other'].append(ret_msg)
        for i in range(5):
            self.model.generate_text(messages)
            ret_msg = self.model.get_respond_content()
            if '该Reset存在问题' in ret_msg:
                msg_dict['error'].append(ret_msg)
            elif '不存在问题' in ret_msg:
                msg_dict['success'].append(ret_msg)
            else:
                msg_dict['other'].append(ret_msg)
        # 如果error次数超过3次，返回error
        if len(msg_dict['error']) > 4:
            return msg_dict['error'][0]
        # 如果other次数超过3次，返回other
        if len(msg_dict['other']) > 4:
            return msg_dict['other'][0]
        return msg_dict['success'][0]





