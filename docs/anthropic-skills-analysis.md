# 引入 anthropics/skills 的分析报告

> 分析对象: https://github.com/anthropics/skills (main 分支, 约 4.4 MB)
> 分析日期: 2025-08-11

## 1. 仓库概况

仓库包含 **17 个技能**（每个一个文件夹，含 `SKILL.md` + 可选的
`scripts/` / `references/` / `templates/` / `assets/`），另有
`spec/`（Agent Skills 规范）、`template/`（技能模板）和
`.claude-plugin/`（Claude Code 插件市场配置）。

技能分四类:

| 类别 | 技能 |
|---|---|
| 创意与设计 | algorithmic-art, canvas-design, frontend-design, theme-factory, web-artifacts-builder |
| 开发与技术 | claude-api, mcp-builder, skill-creator, webapp-testing |
| 企业沟通 | brand-guidelines, internal-comms, doc-coauthoring, slack-gif-creator |
| 文档处理 | docx, pdf, pptx, xlsx |

许可情况: 绝大多数为 **Apache-2.0**；`docx`/`pdf`/`pptx`/`xlsx` 为
**专有许可**（source-available，README 明确说明"非开源"）。

## 2. 本系统的技能模型（适配前提）

- 技能 = `skills/*.md` 单个 markdown 文件，frontmatter 只解析
  `name` / `description`（`src/harness/skills/registry.py`），body 注入
  system prompt。
- 加载器只 glob `skills/` 下**顶层 `*.md`**，不支持文件夹 / 附属脚本。
- 已有 `create_skill` 工具（自进化技能）与 MCP 客户端、bash 沙箱、
  web UI、DeepSeek(OpenAI 兼容) provider。

因此 anthropic 技能若要引入，要么**把 SKILL.md 转成单文件**（脚本另行
vendor 或内嵌路径），要么**扩展加载器支持文件夹技能**（SKILL.md +
scripts/）。

## 3. 分档评估

### A 档: 高价值, 可直接引入（Apache-2.0, 纯指令/轻依赖）✅

| 技能 | 与本系统的契合点 | 引入成本 |
|---|---|---|
| **skill-creator** | 本系统有 `create_skill` 自进化技能；该技能教如何撰写/优化/评估技能（含 evals 与 description 触发优化），形成闭环 | 低: SKILL.md ~33KB 纯指令 + references，无第三方依赖 |
| **doc-coauthoring** | 结构化文档协作流程（提案/spec/决策文档），通用写作场景 | 低: 纯指令，零依赖 |
| **mcp-builder** | 本系统已内置 MCP 客户端（stdio + HTTP）；该技能教用 FastMCP 构建高质量 MCP server，构建→注册→测试闭环 | 低: SKILL.md 指导性 + 少量脚本（`fastmcp` 依赖），建议扩展加载器后引入 |
| **theme-factory** | 为 artifacts（网页/幻灯片/报告）套 10 套预设主题，可配合前端设计与 web UI | 低: themes 为 JSON 资源，无 Python 依赖 |
| **frontend-design** | 本系统 web UI 前端为手写 HTML/CSS/JS；该技能提供排版/字体/设计决策指导 | 低: 纯指导性，零依赖 |

### B 档: 有价值, 但需扩展加载器或安装较重依赖 🟡

| 技能 | 价值 | 障碍 |
|---|---|---|
| **webapp-testing** | Playwright 驱动本地 web 应用测试/截图/看日志；本系统有 web UI 与 bash | 需装 `playwright` + 浏览器；SKILL.md 引用 `scripts/*.py`，需 folder 支持 |
| **slack-gif-creator** | 生成 Slack 优化 GIF | 需 pillow/imageio/imageio-ffmpeg/numpy + 系统 ffmpeg；价值取决于使用场景 |
| **algorithmic-art** | p5.js 生成艺术（flow fields 等） | 纯前端输出，需在浏览器/HTML 中展示；依赖 templates 资源 |
| **canvas-design** | 生成 .png/.pdf 视觉作品 | 需要字体资源（canvas-fonts）；产出为静态图 |
| **web-artifacts-builder** | React/Tailwind/shadcn 多组件 artifact | 面向 claude.ai artifact 运行时，本系统无该运行时；可借鉴其独立 HTML 生成模式，直接价值有限 |

### C 档: 不建议引入（provider/品牌特定 或 专有许可）❌

| 技能 | 原因 |
|---|---|
| **claude-api** | 本系统使用 DeepSeek(OpenAI 兼容)。该技能 frontmatter 明确写"处理 OpenAI/GPT/Gemini 等其它 provider 时 SKIP"，与现 provider 冲突 |
| **brand-guidelines** | Anthropic 品牌色/字体规范，非本系统品牌 |
| **internal-comms** | Anthropic 公司内部沟通模板（status reports、3P updates 等），公司特定格式 |
| **docx / pdf / pptx / xlsx** | 功能上最实用（生产级文档技能，正是 Claude 文档能力背后的实现），但: ① 专有许可（source-available 非开源）需评估合规；② 依赖重: docx 需 `office` helper + defusedxml、pdf 需 pdf2image/pypdf/reportlab/pymupdf、xlsx 需 LibreOffice 重算公式、pptx 需 python-pptx；③ 结构为 scripts 文件夹 + 相对路径引用，需加载器扩展 |

## 4. 落地建议（推荐顺序）

1. **第一批（零成本, 立即引入）**: `skill-creator`、`doc-coauthoring`、
   `frontend-design` — 转成单文件 .md 放入 `skills/`，重启即生效。
2. **第二批（小改造）**: `theme-factory`、`mcp-builder` — 前者 vendor
   themes 资源并改写脚本路径；后者在引入前先扩展加载器。
3. **加载器扩展（可选, 解锁 B 档）**: 在 `SkillRegistry` 增加对
   `SKILL.md` 文件夹的发现（`glob("*/SKILL.md")`），并把 `scripts/`
   目录暴露为技能根路径，即可整体引入 `webapp-testing`、
   `slack-gif-creator`、`algorithmic-art`、`canvas-design`。
4. **文档技能（按需 + 法务确认后）**: `pdf` 单独引入价值最高（纯工具类、
   依赖可控）; docx/pptx/xlsx 建议待专有许可评估通过且用户确有办公文档
   需求时再引入。

依赖均可通过 `uv add`（清华镜像）安装, 无需改动 provider。
