import type { Act, LocationId, LocationState, MockStoryState } from '../mock/story';

export interface StoryEntry {
  id: number;
  round: number;
  title: string;
  text: string;
  chronicle: string;
  type: Act['type'];
  location: LocationId;
  involved: string[];
}

export interface LocationStorySection {
  location: LocationState;
  entries: StoryEntry[];
}

export function toStoryEntry(act: Act): StoryEntry {
  return {
    id: act.id,
    round: act.round,
    title: `第${act.round}轮`,
    text: act.narrative,
    chronicle: act.chronicle,
    type: act.type,
    location: act.location,
    involved: act.involved,
  };
}

export function getLocationStorySections(story: MockStoryState, limitPerLocation = 4): LocationStorySection[] {
  return story.locations.map((location) => ({
    location,
    entries: story.acts
      .filter((act) => act.location === location.id)
      .slice(-limitPerLocation)
      .map(toStoryEntry),
  }));
}

export function getMobileStoryEntries(story: MockStoryState, limit = 7): StoryEntry[] {
  return story.acts.slice(-limit).map(toStoryEntry);
}

export function getPersonalStoryEntries(
  story: MockStoryState,
  charId: string,
  location: LocationId,
  limit = 6,
): StoryEntry[] {
  return story.acts
    .filter((act) => act.involved.includes(charId) || act.location === location)
    .slice(-limit)
    .map(toStoryEntry);
}
