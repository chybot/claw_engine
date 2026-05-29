---
name: dag_tracer
description: >-
  诊断商品搜索 trace 的 DAG 漏斗与 item 状态。
  Minimal fixture extracted from algo-bot-skills/algo/dag_tracer/ for the P7
  portability proof. The full skill lives in the algo-bot-skills repository;
  this fixture preserves the file shape (frontmatter + body + scripts/) the V1
  SkillProvisioner must handle.
---

# DAG Tracer (test fixture)

Provisioned by `SkillProvisioner` to `cwd/.agents/skills/dag_tracer/` to prove
the V1 chain can land a real-shape skill on disk. The fixture is intentionally
minimal — full diagnostic logic lives upstream.
