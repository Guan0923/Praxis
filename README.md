# Praxis

**Turn ideas into action. On your machine. On your terms.**

Praxis is a personal AI workspace that runs locally and opens in your browser. Give it a task, bring in your files, and work through it together: inspect a project, change code, run commands, or gather information from the web. Follow the work as it happens, with plans, tool results, and permission decisions in the conversation.

**English** | [简体中文](README.zh-CN.md)

<!-- DEMO: Add docs/assets/praxis-demo.gif, then uncomment the image below.
![Praxis: from a task to a plan, tool execution, and a finished result](docs/assets/praxis-demo.gif)
-->

## Bring a task, not just a question

A useful assistant should help you move the work forward. Here are a few things to try with your own projects:

| Start with... | Work toward... |
| --- | --- |
| "Explain how this project starts, and point me to the important files." | A guided tour grounded in the code in front of you. |
| "Find the cause of this error. Propose a fix before changing anything." | A plan you can discuss, then an implementation you can inspect. |
| "Read these logs and write a short report of the recurring failures." | A result saved in your workspace, not only an answer in chat. |
| "Look up the documentation for this feature and compare the approaches." | Web research brought back into the context of your task. |

These are example tasks, not benchmark results. What Praxis can complete depends on your model, available tools, and the permissions you grant.

## Stay close to the work

### Think it through. Then put it to work.

Use **Plan mode** to explore and discuss before making changes. Switch to **Agent mode** when you are ready for execution. Files, commands, and results stay connected to the conversation, so you can see what happened and decide what comes next.

### Your projects, with their context attached

Keep separate projects and conversations, reference files in a message, and inspect workspace files in the side panel. Continue a discussion or explore another direction without squeezing everything into one endless chat.

### Make it fit the way you work

Connect your chosen model service in Settings. Add **Skills** for reusable instructions and workflows, or connect **MCP servers** to bring in external tools. Project-supplied instructions and external tools still go through the relevant trust and approval checks.

### Local by design, explicit about access

Praxis does not require a Praxis account or a cloud-sync service. Application data lives on your machine. Tool access is checked against workspace boundaries and approval rules, and strict sandbox execution does not silently fall back to an unrestricted process.

## Quick start

The development workflow below targets **Windows**. Command isolation uses a Windows sandbox service; do not assume equivalent sandbox support on other operating systems.

You will need **Python 3.11+**, **Node.js 20+**, **uv**,. If you use the project's Conda setup, run `conda activate dev` first. You also need a model service you are authorized to use.

From the repository root:

```powershell
uv sync --locked
cd frontend
npm ci
cd ..
uv run python -m backend.api
```

In a second terminal, from the repository root:

```powershell
cd frontend
npm run dev
```

Open **<http://127.0.0.1:5173>**, then:

1. Open **Settings** and configure your model provider, endpoint, model, and API key where required.
2. Check the **Sandbox** settings. Installing the Windows sandbox requires administrator approval. Commands that require it remain blocked until it is ready.
3. Create a conversation or open a project, reference the relevant files, and describe a task. Start in Plan mode when you want to agree on the approach first.

Run one backend process. Queued messages and temporary state live only in that process; restarting it discards them. SQLite retains formal history.

For a single-server setup, build the frontend with `npm run build` inside `frontend/`, then run the backend. It serves the built app at <http://127.0.0.1:8000>.

## Your data and your permissions

- **Stored locally:** configuration, projects, conversation history, and workspace files use `~/.praxis`. Provider API keys are encrypted before storage; they are not kept in the configuration TOML file.
- **Local does not mean offline:** an online model receives the relevant request content. Web tools and external MCP servers may also send data outside your machine. Choose services and permissions to match the task.
- **No application login:** Praxis is a local, single-user app, not a shared internet-facing service. Model providers and external tools may require their own credentials.
- **A fresh installation identity:** this release does not import previous installation data or credentials. Existing users need to configure Praxis again; see the [installation notes](docs/development.md#praxis-installation).

## Go deeper

| Guide | What you will find |
| --- | --- |
| [Development](docs/development.md) | Setup, local data, troubleshooting, and verification commands. |
| [Architecture](docs/architecture.md) | How the browser, runtime, tools, and storage fit together. |
| [Backend](backend/README.md) | Local APIs, model connections, MCP, and sandbox details. |
| [Frontend](frontend/README.md) | Browser client structure and development scripts. |
| [Benchmarks](benchmarks/README.md) | The task suite and the limits of its scores. |

Have a workflow to improve? Open an issue with the task, what you expected, and what happened, with secrets and private files removed. For code changes, start with the development guide and include the checks you ran.
