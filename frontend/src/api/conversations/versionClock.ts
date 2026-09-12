// Protect an in-flight history read from a version choice made after it started.
let clock = 0;
const choices = new Map<string, { clock: number; index: number }>();
export const versionReadClock = () => clock;
export function recordVersionChoice(session: string, turn: string, index: number) {
  choices.set(`${session}:${turn}`, { clock: ++clock, index });
}
export function versionAfterRead(session: string, turn: string, started: number) {
  const choice = choices.get(`${session}:${turn}`);
  return choice && choice.clock > started ? choice.index : undefined;
}
