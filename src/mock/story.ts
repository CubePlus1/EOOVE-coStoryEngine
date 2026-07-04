export type HopeHint = 'low' | 'mid' | 'high';
export type WorldStatus = 'running' | 'settling';
export type ActKind = 'action' | 'narration' | 'oracle';
export type ActType = 'act' | 'node' | 'settlement';
export type LocationId = 'loc_shelter' | 'loc_ruins' | 'loc_observatory';
export type ActScope = 'global' | LocationId;

export interface WorldState {
  cycle: number;
  round: number;
  maxRound: number;
  hopeHint: HopeHint;
  status: WorldStatus;
}

export interface LocationState {
  id: LocationId;
  name: string;
  directive: {
    situation: string;
    hint: string;
  };
}

export interface Act {
  id: number;
  cycle: number;
  round: number;
  location: LocationId;
  type: ActType;
  narrative: string;
  chronicle: string;
  involved: string[];
  importance: number;
}

export interface Character {
  charId: string;
  name: string;
  type: 'human' | 'ai';
  status: 'active' | 'leaving' | 'ended';
  location: LocationId;
}

export interface OraclePoolItem {
  cycle: number;
  round: number;
  scope: ActScope;
  text: string;
  charId: string;
}

export interface MockStoryState {
  world: WorldState;
  locations: LocationState[];
  acts: Act[];
  characters: Character[];
  oraclePool: OraclePoolItem[];
  appliedOracles: Set<string>;
  nextActId: number;
}

export interface SubmitActInput {
  charId: string;
  text: string;
  kind: ActKind;
  scope: ActScope;
}

export type SubmitActResult =
  | { accepted: true; round: number; oracleStatus?: 'applied' | 'pooled' }
  | { accepted: false; reason: string };

export function createMockWorld(): MockStoryState;
export function createMockWorld(override: Partial<WorldState>): MockStoryState;
export function createMockWorld(override: Partial<WorldState> = {}): MockStoryState {
  const world: WorldState = {
    cycle: 2,
    round: 37,
    maxRound: 60,
    hopeHint: 'low',
    status: 'running',
    ...override,
  };

  const acts: Act[] = [
    {
      id: 100,
      cycle: world.cycle,
      round: Math.max(1, world.round - 2),
      location: 'loc_observatory',
      type: 'act',
      narrative:
        '星图修士把不存在的星座摹在黑绢上, 每一颗星都像上一轮回留下的针孔。天象仪没有回答, 只把铜针转向地下。',
      chronicle: `${roundTitle(Math.max(1, world.round - 2))}, 观测站录得逆星。`,
      involved: ['c_04'],
      importance: 4,
    },
    {
      id: 101,
      cycle: world.cycle,
      round: Math.max(1, world.round - 1),
      location: 'loc_ruins',
      type: 'act',
      narrative:
        '林拾荒者撬开了那扇锈门, 敲击声戛然而止。门后是一间完好的水泵房, 墙上用旧世界的文字写着: 泵还活着, 人还没死绝。',
      chronicle: `${roundTitle(Math.max(1, world.round - 1))}, 废墟深处, 拾荒者寻得活泵。`,
      involved: ['c_01'],
      importance: 4,
    },
    {
      id: 102,
      cycle: world.cycle,
      round: world.round,
      location: 'loc_shelter',
      type: 'act',
      narrative:
        '神谕降下: 让孩子们唱歌。歌声在幻觉蔓延的走廊里响起, 恐慌像退潮一样安静下去。',
      chronicle: `${roundTitle(world.round)}, 歌声抚平避难所。`,
      involved: ['c_02', 'c_03'],
      importance: 3,
    },
  ];

  return {
    world,
    locations: [
      {
        id: 'loc_shelter',
        name: '避难所',
        directive: {
          situation: '地下水源正在枯竭, 居民出现幻觉',
          hint: '找到复苏水源的方法, 或安抚恐慌的居民',
        },
      },
      {
        id: 'loc_ruins',
        name: '废墟',
        directive: {
          situation: '废墟深处传出规律的敲击声',
          hint: '查明声源, 它可能是希望也可能是灾祸',
        },
      },
      {
        id: 'loc_observatory',
        name: '观测站',
        directive: {
          situation: '天象仪指向了不存在的星座',
          hint: '破译星图, 异象的答案藏在其中',
        },
      },
    ],
    acts,
    characters: [
      {
        charId: 'c_01',
        name: '林拾荒者',
        type: 'human',
        status: 'active',
        location: 'loc_ruins',
      },
      {
        charId: 'c_02',
        name: '守夜人梅',
        type: 'ai',
        status: 'active',
        location: 'loc_shelter',
      },
      {
        charId: 'c_03',
        name: '小满',
        type: 'human',
        status: 'active',
        location: 'loc_shelter',
      },
      {
        charId: 'c_04',
        name: '星图修士',
        type: 'ai',
        status: 'active',
        location: 'loc_observatory',
      },
    ],
    oraclePool: [],
    appliedOracles: new Set<string>(),
    nextActId: 103,
  };
}

export function submitMockAct(world: MockStoryState, input: SubmitActInput): SubmitActResult {
  if (world.world.status !== 'running') {
    return { accepted: false, reason: '世界正在被审判, 请等下一次钟声。' };
  }

  const character = world.characters.find((item) => item.charId === input.charId);
  if (!character || character.status !== 'active') {
    return { accepted: false, reason: '史官找不到这个名字。' };
  }

  const text = input.text.trim();
  if (text.length < 2) {
    return { accepted: false, reason: '命运没有听清, 请留下更完整的一句话。' };
  }

  const scope = input.kind === 'oracle' ? input.scope : character.location;
  let oracleStatus: 'applied' | 'pooled' | undefined;
  if (input.kind === 'oracle') {
    const oracleKey = `${world.world.cycle}:${world.world.round}:${scope}`;
    if (world.appliedOracles.has(oracleKey)) {
      oracleStatus = 'pooled';
      world.oraclePool.push({
        cycle: world.world.cycle,
        round: world.world.round,
        scope,
        text,
        charId: input.charId,
      });
    } else {
      oracleStatus = 'applied';
      world.appliedOracles.add(oracleKey);
    }
  }

  world.world.round += 1;
  const location = scope === 'global' ? character.location : scope;
  const act: Act = {
    id: world.nextActId,
    cycle: world.world.cycle,
    round: world.world.round,
    location,
    type: 'act',
    narrative: weaveNarrative(character.name, input.kind, text, location),
    chronicle: `${roundTitle(world.world.round)}, ${locationLabel(location)}, ${chronicleVerb(input.kind, character.name)}。`,
    involved: [character.charId],
    importance: input.kind === 'oracle' ? 5 : 3,
  };

  world.nextActId += 1;
  if (oracleStatus !== 'pooled') {
    world.acts.push(act);
  }
  applyHopeDrift(world, input.kind, text);
  maybeAppendNodeEvent(world);
  maybeSettleWorld(world);

  return {
    accepted: true,
    round: world.world.round,
    ...(oracleStatus ? { oracleStatus } : {}),
  };
}

export function tickMockWorld(world: MockStoryState): void {
  if (world.world.status !== 'running') {
    return;
  }

  world.world.round += 1;
  const location = world.locations[(world.world.round + world.world.cycle) % world.locations.length];
  const character = world.characters.find((item) => item.location === location.id && item.status === 'active');
  const act: Act = {
    id: world.nextActId,
    cycle: world.world.cycle,
    round: world.world.round,
    location: location.id,
    type: 'act',
    narrative: `${character?.name ?? location.name}在废墟钟声里留下新的证词, 旧纸页边缘浮出一道细金。`,
    chronicle: `${roundTitle(world.world.round)}, ${location.name}添入一页旁注。`,
    involved: character ? [character.charId] : [],
    importance: 2,
  };

  world.nextActId += 1;
  world.acts.push(act);
  maybeAppendNodeEvent(world);
  maybeSettleWorld(world);
}

function maybeAppendNodeEvent(world: MockStoryState): void {
  const node = nodeEvents[world.world.round];
  if (!node) {
    return;
  }

  world.locations = world.locations.map((location) => ({
    ...location,
    directive: {
      situation: node.situation,
      hint: `${location.name}必须立刻执行节点令: ${node.hint}`,
    },
  }));

  world.acts.push({
    id: world.nextActId,
    cycle: world.world.cycle,
    round: world.world.round,
    location: 'loc_observatory',
    type: 'node',
    narrative: node.narrative,
    chronicle: `${roundTitle(world.world.round)}, ${node.chronicle}`,
    involved: [],
    importance: 9,
  });
  world.nextActId += 1;
}

function maybeSettleWorld(world: MockStoryState): void {
  if (world.world.round < world.world.maxRound) {
    return;
  }

  world.world.status = 'settling';
  const saved = world.world.hopeHint === 'high';
  world.acts.push({
    id: world.nextActId,
    cycle: world.world.cycle,
    round: world.world.maxRound,
    location: 'loc_observatory',
    type: 'settlement',
    narrative: saved
      ? '终章落笔时, 星图没有熄灭。幸存者把彼此的名字刻进铜页, 等待下一次轮回醒来。'
      : '终章落笔时, 城市像灰烬一样合拢。最后的火种被封入黑匣, 留给下一次轮回审读。',
    chronicle: saved ? '第六十轮, 众名得存, 新纪元启封。' : '第六十轮, 旧世归尘, 火种入匣。',
    involved: world.characters.filter((character) => character.status === 'active').map((character) => character.charId),
    importance: 10,
  });
  world.nextActId += 1;
  world.characters = world.characters.map((character) =>
    character.type === 'human' ? { ...character, status: 'ended' } : character,
  );
  world.world = {
    ...world.world,
    cycle: world.world.cycle + 1,
    round: 0,
    hopeHint: 'mid',
    status: 'running',
  };
  world.appliedOracles.clear();
}

function applyHopeDrift(world: MockStoryState, kind: ActKind, text: string): void {
  const goodWords = ['救', '复苏', '唱歌', '修', '希望', '安抚', '水'];
  const badWords = ['烧', '杀', '毁', '阻止', '沉默', '裂'];
  const score =
    goodWords.filter((word) => text.includes(word)).length -
    badWords.filter((word) => text.includes(word)).length +
    (kind === 'oracle' ? 1 : 0);

  if (score > 0) {
    world.world.hopeHint = world.world.hopeHint === 'low' ? 'mid' : 'high';
  }
  if (score < 0) {
    world.world.hopeHint = world.world.hopeHint === 'high' ? 'mid' : 'low';
  }
}

function weaveNarrative(name: string, kind: ActKind, text: string, location: LocationId): string {
  if (kind === 'oracle') {
    return `神谕从${locationLabel(location)}上空垂落: ${text} 史官把这句话压入铜印, 等待世界应验。`;
  }
  if (kind === 'narration') {
    return `旁白写下: ${text} ${name}的影子被烛火拉长, 像旧纪年册里刚醒来的墨迹。`;
  }
  return `${name}在${locationLabel(location)}行动: ${text} 纸页轻震, 新的轮数被烙进岁月史书。`;
}

function chronicleVerb(kind: ActKind, name: string): string {
  if (kind === 'oracle') {
    return `${name}请下神谕`;
  }
  if (kind === 'narration') {
    return `${name}旁注命运`;
  }
  return `${name}改写一页`;
}

function locationLabel(location: LocationId): string {
  return {
    loc_shelter: '避难所',
    loc_ruins: '废墟',
    loc_observatory: '观测站',
  }[location];
}

function roundTitle(round: number): string {
  return `第${toChineseNumeral(round)}轮`;
}

function toChineseNumeral(value: number): string {
  const digits = ['零', '一', '二', '三', '四', '五', '六', '七', '八', '九'];
  if (value <= 10) {
    return value === 10 ? '十' : digits[value];
  }
  if (value < 20) {
    return `十${digits[value % 10]}`;
  }
  const tens = Math.floor(value / 10);
  const ones = value % 10;
  return `${digits[tens]}十${ones === 0 ? '' : digits[ones]}`;
}

const nodeEvents: Partial<Record<number, Pick<LocationState['directive'], 'situation' | 'hint'> & {
  narrative: string;
  chronicle: string;
}>> = {
  20: {
    situation: '天幕初裂, 远处的白昼像纸灰一样倒卷',
    hint: '记录第一道异象, 不要让见证者独自沉默',
    narrative: '第二十轮, 天空被无形的笔锋划开。每个地点都看见同一道裂光, 没有人能够把它擦去。',
    chronicle: '天幕初裂, 三地同见白痕。',
  },
  40: {
    situation: '旧文明遗物同时鸣响, 地下传来审判般的低频',
    hint: '决定保留什么, 放弃什么',
    narrative: '第四十轮, 旧文明向幸存者讨还债务。铁塔俯身, 水渠反流, 星盘停止。',
    chronicle: '文明反噬, 铜钟自鸣。',
  },
  60: {
    situation: '毁灭临门, 所有名字等待终章裁定',
    hint: '写下最后一件值得被下一世记住的事',
    narrative: '第六十轮, 世界把最后一页摊开。光与灰同时抵达, 所有行动都被叫到史官面前。',
    chronicle: '终章启封, 万名受审。',
  },
};
