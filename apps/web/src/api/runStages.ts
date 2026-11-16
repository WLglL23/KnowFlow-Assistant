// Historical runs can contain this persisted stage.
const legacyStageLabels = { RETRIEVE_2: "补充检索" } as const;

const runStageLabels = {
  UNDERSTAND: "理解问题",
  PLAN: "规划检索",
  RETRIEVE_1: "检索资料",
  VERIFY: "核验证据",
  GENERATE: "生成回答",
  ...legacyStageLabels,
} as const;

export type RunStage = keyof typeof runStageLabels;

export function stageLabel(stage: string): string {
  return runStageLabels[stage as RunStage] ?? stage;
}
