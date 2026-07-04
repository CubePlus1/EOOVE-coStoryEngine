import { describe, expect, it } from 'vitest';
import { createMockWorld, submitMockAct, tickMockWorld } from './story';

describe('mock story engine', () => {
  it('advances an accepted action into a new act and chronicle line', () => {
    const world = createMockWorld();

    const result = submitMockAct(world, {
      charId: 'c_01',
      kind: 'action',
      text: '我把旧水泵重新接上铜线。',
      scope: 'loc_ruins',
    });

    expect(result.accepted).toBe(true);
    expect(world.world.round).toBe(38);
    expect(world.acts.at(-1)?.chronicle).toContain('第三十八轮');
    expect(world.acts.at(-1)?.involved).toEqual(['c_01']);
  });

  it('pools the second oracle in the same cycle, round, and scope', () => {
    const world = createMockWorld();

    const first = submitMockAct(world, {
      charId: 'c_02',
      kind: 'oracle',
      text: '让孩子们唱歌。',
      scope: 'loc_shelter',
    });
    world.world.round = 37;
    const second = submitMockAct(world, {
      charId: 'c_03',
      kind: 'oracle',
      text: '让灯火向地下延伸。',
      scope: 'loc_shelter',
    });

    if (!first.accepted || !second.accepted) {
      throw new Error('Expected both oracle submissions to be accepted');
    }
    expect(first.accepted).toBe(true);
    expect(first.oracleStatus).toBe('applied');
    expect(second.accepted).toBe(true);
    expect(second.oracleStatus).toBe('pooled');
    expect(world.oraclePool).toHaveLength(1);
  });

  it('creates node events at fixed rounds without being cancelled by acts', () => {
    const world = createMockWorld({ round: 19 });

    submitMockAct(world, {
      charId: 'c_01',
      kind: 'narration',
      text: '所有人试图阻止异象发生。',
      scope: 'global',
    });

    expect(world.world.round).toBe(20);
    expect(world.acts.at(-1)?.type).toBe('node');
    expect(world.acts.at(-1)?.chronicle).toContain('天幕初裂');
  });

  it('rolls over into the next cycle after settlement', () => {
    const world = createMockWorld({ round: 59, hopeHint: 'high' });

    tickMockWorld(world);

    expect(world.world.round).toBe(0);
    expect(world.world.cycle).toBe(3);
    expect(world.world.status).toBe('running');
    expect(world.acts.some((act) => act.type === 'settlement')).toBe(true);
  });
});
