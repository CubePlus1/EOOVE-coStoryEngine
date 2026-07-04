import { describe, expect, it } from 'vitest';
import { createMockWorld } from '../mock/story';
import {
  getLocationStorySections,
  getMobileStoryEntries,
  getPersonalStoryEntries,
  toStoryEntry,
} from './storyEntries';

describe('story entry view model', () => {
  it('normalizes an act into one display entry shape', () => {
    const story = createMockWorld();
    const act = story.acts[0];

    const entry = toStoryEntry(act);

    expect(entry).toEqual({
      id: act.id,
      round: act.round,
      title: `第${act.round}轮`,
      text: act.narrative,
      chronicle: act.chronicle,
      type: act.type,
      location: act.location,
      involved: act.involved,
    });
  });

  it('groups wall entries by location with one normalized shape', () => {
    const story = createMockWorld();

    const sections = getLocationStorySections(story, 2);

    expect(sections).toHaveLength(story.locations.length);
    expect(sections.map((section) => section.location.id)).toEqual(story.locations.map((location) => location.id));
    expect(sections.every((section) => section.entries.length <= 2)).toBe(true);
    expect(sections.flatMap((section) => section.entries).every((entry) => entry.title.startsWith('第'))).toBe(true);
  });

  it('uses the same entry shape for mobile world and personal feeds', () => {
    const story = createMockWorld();

    const worldEntries = getMobileStoryEntries(story, 7);
    const personalEntries = getPersonalStoryEntries(story, 'c_01', 'loc_ruins', 6);

    expect(worldEntries).toHaveLength(Math.min(story.acts.length, 7));
    expect(personalEntries.every((entry) => entry.involved.includes('c_01') || entry.location === 'loc_ruins')).toBe(true);
    expect(worldEntries[0]).toHaveProperty('text');
    expect(personalEntries[0]).toHaveProperty('chronicle');
  });
});
