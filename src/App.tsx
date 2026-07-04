import { useEffect, useMemo, useState } from 'react';
import {
  BookOpenText,
  ChevronRight,
  Flame,
  Hourglass,
  Landmark,
  MessageSquareText,
  MoonStar,
  ScrollText,
  Send,
  ShieldAlert,
  Sparkles,
} from 'lucide-react';
import { AnimatePresence, motion } from 'motion/react';
import {
  type Act,
  type ActKind,
  type LocationId,
  type MockStoryState,
  type SubmitActResult,
  createMockWorld,
  submitMockAct,
  tickMockWorld,
} from './mock/story';

type ViewMode = 'phone' | 'wall' | 'card';
type PhonePhase = 'gate' | 'join' | 'reveal' | 'world';
type StoryTab = 'world' | 'mine';

const currentCharId = 'c_01';

export function App() {
  const [view, setView] = useState<ViewMode>(() => {
    const viewParam = new URLSearchParams(window.location.search).get('view');
    return viewParam === 'phone' || viewParam === 'card' || viewParam === 'wall' ? viewParam : 'wall';
  });
  const [story, setStory] = useState<MockStoryState>(() => createMockWorld());
  const [phase, setPhase] = useState<PhonePhase>(() => (localStorage.getItem('world_charId') ? 'world' : 'gate'));
  const [tab, setTab] = useState<StoryTab>('world');
  const [draft, setDraft] = useState('我沿着旧水管寻找还能发声的阀门。');
  const [actKind, setActKind] = useState<ActKind>('action');
  const [scope, setScope] = useState<'global' | LocationId>('loc_ruins');
  const [notice, setNotice] = useState('史书仍在翻动, 等待下一笔。');
  const [nodeFlash, setNodeFlash] = useState<Act | null>(null);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (document.hidden) {
        return;
      }
      setStory((previous) => {
        const next = cloneStory(previous);
        const before = next.acts.at(-1)?.id;
        tickMockWorld(next);
        const latest = next.acts.at(-1);
        if (latest && latest.id !== before && (latest.type === 'node' || latest.type === 'settlement')) {
          setNodeFlash(latest);
          window.setTimeout(() => setNodeFlash(null), 4200);
        }
        return next;
      });
    }, 5200);
    return () => window.clearInterval(interval);
  }, []);

  const me = story.characters.find((character) => character.charId === currentCharId) ?? story.characters[0];
  const myLocation = story.locations.find((location) => location.id === me.location) ?? story.locations[0];
  const myActs = story.acts.filter((act) => act.involved.includes(currentCharId) || act.location === me.location).slice(-6);
  const progress = Math.round((story.world.round / story.world.maxRound) * 100);
  const groupedActs = useMemo(() => {
    return story.locations.map((location) => ({
      location,
      acts: story.acts.filter((act) => act.location === location.id).slice(-4),
    }));
  }, [story]);

  function switchView(nextView: ViewMode) {
    setView(nextView);
    const url = new URL(window.location.href);
    url.searchParams.set('view', nextView);
    window.history.replaceState(null, '', url);
  }

  function enterWorld() {
    setPhase('join');
  }

  function joinWorld() {
    localStorage.setItem('world_charId', currentCharId);
    setNotice('世界已经认出你的名字。');
    setPhase('reveal');
  }

  function revealDone() {
    setPhase('world');
  }

  function submitAct() {
    setStory((previous) => {
      const next = cloneStory(previous);
      const result = submitMockAct(next, {
        charId: currentCharId,
        text: draft,
        kind: actKind,
        scope,
      });
      setNotice(formatSubmitNotice(result));
      if (result.accepted) {
        setDraft('');
        const latest = next.acts.at(-1);
        if (latest?.type === 'node' || latest?.type === 'settlement') {
          setNodeFlash(latest);
          window.setTimeout(() => setNodeFlash(null), 4200);
        }
      }
      return next;
    });
  }

  return (
    <main className={`chronicle-shell hope-${story.world.hopeHint}`}>
      <div className="grain" aria-hidden="true" />
      <div className="star-map" aria-hidden="true" />

      <nav className="mode-switch" aria-label="展示模式">
        <button className={view === 'phone' ? 'active' : ''} type="button" onClick={() => switchView('phone')}>
          <MessageSquareText size={17} />
          H5
        </button>
        <button className={view === 'wall' ? 'active' : ''} type="button" onClick={() => switchView('wall')}>
          <Landmark size={17} />
          大屏
        </button>
        <button className={view === 'card' ? 'active' : ''} type="button" onClick={() => switchView('card')}>
          <ScrollText size={17} />
          结局卡
        </button>
      </nav>

      <AnimatePresence mode="wait">
        {view === 'wall' && (
          <motion.section
            key="wall"
            className="wall-stage"
            initial={{ opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -16 }}
            transition={{ duration: 0.55, ease: [0.16, 1, 0.3, 1] }}
          >
            <WallHeader story={story} progress={progress} />
            <section className="location-grid">
              {groupedActs.map(({ location, acts }, index) => (
                <motion.article
                  className="location-panel"
                  key={location.id}
                  initial={{ opacity: 0, y: 34 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: index * 0.08, duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
                >
                  <div className="panel-crest">
                    <span>{location.name}</span>
                    <small>{location.directive.situation}</small>
                  </div>
                  <p className="directive">{location.directive.hint}</p>
                  <div className="typewriter-list">
                    {acts.map((act) => (
                      <motion.div
                        className={`act-line ${act.type}`}
                        key={act.id}
                        layout
                        initial={{ opacity: 0, x: 18 }}
                        animate={{ opacity: 1, x: 0 }}
                      >
                        <strong>第{act.round}轮</strong>
                        <p>{act.narrative}</p>
                      </motion.div>
                    ))}
                  </div>
                </motion.article>
              ))}
            </section>
            <ChronicleRibbon acts={story.acts} />
            <CharacterWall story={story} />
          </motion.section>
        )}

        {view === 'phone' && (
          <motion.section
            key="phone"
            className="phone-scene"
            initial={{ opacity: 0, scale: 0.98 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.98 }}
            transition={{ duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
          >
            <div className="phone-frame">
              <AnimatePresence mode="wait">
                {phase === 'gate' && (
                  <motion.div className="phone-page gate-page" key="gate" {...pageMotion}>
                    <MoonStar size={30} />
                    <p className="eyebrow">AI共创轮回世界</p>
                    <h1>
                      岁月史书
                      <span>缺一位见证者</span>
                    </h1>
                    <p>第{story.world.cycle}轮回 · 第{story.world.round}轮 · 毁灭将至</p>
                    <button className="primary-action" type="button" onClick={enterWorld}>
                      进入世界
                      <ChevronRight size={18} />
                    </button>
                  </motion.div>
                )}

                {phase === 'join' && (
                  <motion.div className="phone-page join-page" key="join" {...pageMotion}>
                    <p className="eyebrow">写下自述</p>
                    <label>
                      用一句话描述你自己
                      <textarea defaultValue="靠一双手在废墟里活了十年, 不信神, 只信工具。" />
                    </label>
                    <label>
                      邮箱
                      <input placeholder="可选, 角色死后会给你写信" />
                    </label>
                    <button className="primary-action" type="button" onClick={joinWorld}>
                      世界正在感知你
                      <Sparkles size={18} />
                    </button>
                  </motion.div>
                )}

                {phase === 'reveal' && (
                  <motion.div className="phone-page reveal-page" key="reveal" {...pageMotion}>
                    <motion.div
                      className="reveal-card"
                      initial={{ rotateY: 88, opacity: 0 }}
                      animate={{ rotateY: 0, opacity: 1 }}
                      transition={{ duration: 0.9, ease: [0.16, 1, 0.3, 1] }}
                    >
                      <BookOpenText size={24} />
                      <h2>{me.name}</h2>
                      <p>靠一双手在废墟里活了十年, 不信神, 只信工具。</p>
                      <small>地点 · {myLocation.name}</small>
                    </motion.div>
                    <button className="primary-action" type="button" onClick={revealDone}>
                      抬头看大屏
                      <ChevronRight size={18} />
                    </button>
                  </motion.div>
                )}

                {phase === 'world' && (
                  <motion.div className="phone-page world-page" key="world" {...pageMotion}>
                    <div className="mobile-progress">
                      <span>
                        第 {story.world.round}/{story.world.maxRound} 轮
                      </span>
                      <div>
                        <i style={{ width: `${progress}%` }} />
                      </div>
                    </div>
                    <article className="mobile-directive">
                      <strong>{myLocation.name}</strong>
                      <p>{myLocation.directive.situation}</p>
                      <small>{myLocation.directive.hint}</small>
                    </article>
                    <div className="tabs" role="tablist">
                      <button className={tab === 'world' ? 'active' : ''} type="button" onClick={() => setTab('world')}>
                        世界
                      </button>
                      <button className={tab === 'mine' ? 'active' : ''} type="button" onClick={() => setTab('mine')}>
                        我的故事
                      </button>
                    </div>
                    <div className="mobile-feed">
                      {(tab === 'world' ? story.acts.slice(-7) : myActs).map((act) => (
                        <article className="feed-item" key={`${tab}-${act.id}`}>
                          <strong>{act.chronicle}</strong>
                          <p>{act.narrative}</p>
                        </article>
                      ))}
                    </div>
                    <div className="compose">
                      <div className="kind-picker">
                        {(['action', 'narration', 'oracle'] satisfies ActKind[]).map((kind) => (
                          <button className={actKind === kind ? 'active' : ''} key={kind} type="button" onClick={() => setActKind(kind)}>
                            {kindLabel(kind)}
                          </button>
                        ))}
                      </div>
                      {actKind === 'oracle' && (
                        <select value={scope} onChange={(event) => setScope(event.target.value as 'global' | LocationId)}>
                          <option value="loc_ruins">本地</option>
                          <option value="global">全世界</option>
                        </select>
                      )}
                      <textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="你的行动正被编入命运..." />
                      <button className="send-action" type="button" onClick={submitAct} disabled={!draft.trim()}>
                        <Send size={17} />
                        写入命运
                      </button>
                    </div>
                    <p className="notice">{notice}</p>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </motion.section>
        )}

        {view === 'card' && (
          <motion.section key="card" className="ending-scene" {...pageMotion}>
            <article className="ending-card">
              <div className="seal">终章</div>
              <p className="eyebrow">第{story.world.cycle - 1 || 2}轮回 · 幸存者</p>
              <h1>{me.name}</h1>
              <p className="ending-copy">
                你把水泵接回大地的脉搏。下一世的人醒来时, 会听见管道深处仍有你的敲击声。
              </p>
              <div className="qr-mark">EOOVE</div>
              <button className="primary-action" type="button">
                保存长图
                <ScrollText size={18} />
              </button>
            </article>
          </motion.section>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {nodeFlash && (
          <motion.div
            className={`node-takeover ${nodeFlash.type}`}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.35 }}
          >
            <motion.div initial={{ scale: 0.84, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ duration: 0.7 }}>
              {nodeFlash.type === 'settlement' ? <Hourglass size={42} /> : <ShieldAlert size={42} />}
              <h2>{nodeFlash.type === 'settlement' ? '终章落笔' : '节点事件'}</h2>
              <p>{nodeFlash.narrative}</p>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </main>
  );
}

function WallHeader({ story, progress }: { story: MockStoryState; progress: number }) {
  return (
    <header className="wall-header">
      <div>
        <p className="eyebrow">EOOVE · 岁月史书</p>
        <h1>第{story.world.cycle}轮回 · 第{story.world.round}/60轮</h1>
      </div>
      <div className="sky-meter" aria-label={`世界进度 ${progress}%`}>
        <span style={{ width: `${progress}%` }} />
      </div>
      <div className="omen">
        <Flame size={20} />
        <span>{hopeLabel(story.world.hopeHint)}</span>
      </div>
    </header>
  );
}

function ChronicleRibbon({ acts }: { acts: Act[] }) {
  const lines = [...acts.slice(-8), ...acts.slice(-8)];
  return (
    <section className="chronicle-ribbon" aria-label="岁月史书带">
      <div>
        {lines.map((act, index) => (
          <span key={`${act.id}-${index}`}>{act.chronicle}</span>
        ))}
      </div>
    </section>
  );
}

function CharacterWall({ story }: { story: MockStoryState }) {
  return (
    <section className="character-wall" aria-label="角色卡墙">
      {story.characters.map((character, index) => (
        <motion.article
          className={character.status === 'active' ? '' : 'ended'}
          key={character.charId}
          initial={{ opacity: 0, y: 22, scale: 0.92 }}
          animate={{ opacity: 1, y: 0, scale: index === 0 ? 1.08 : 1 }}
          transition={{ delay: index * 0.07, duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        >
          <span>{character.name.slice(0, 1)}</span>
          <strong>{character.name}</strong>
          <small>{character.type === 'human' ? '玩家' : '回声'}</small>
        </motion.article>
      ))}
    </section>
  );
}

function cloneStory(story: MockStoryState): MockStoryState {
  return {
    ...story,
    world: { ...story.world },
    locations: story.locations.map((location) => ({
      ...location,
      directive: { ...location.directive },
    })),
    acts: story.acts.map((act) => ({ ...act, involved: [...act.involved] })),
    characters: story.characters.map((character) => ({ ...character })),
    oraclePool: story.oraclePool.map((item) => ({ ...item })),
    appliedOracles: new Set(story.appliedOracles),
  };
}

function formatSubmitNotice(result: SubmitActResult): string {
  if (!result.accepted) {
    return result.reason;
  }
  if (result.oracleStatus === 'pooled') {
    return '你的意志尚未汇聚成现实, 但已被世界记住。';
  }
  return `已入戏, 第${result.round}轮。`;
}

function kindLabel(kind: ActKind): string {
  return {
    action: '行动',
    narration: '旁白',
    oracle: '神谕',
  }[kind];
}

function hopeLabel(hope: MockStoryState['world']['hopeHint']): string {
  return {
    low: '天色压暗',
    mid: '余烬未熄',
    high: '金光回潮',
  }[hope];
}

const pageMotion = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -14 },
  transition: { duration: 0.45, ease: [0.16, 1, 0.3, 1] },
} as const;
