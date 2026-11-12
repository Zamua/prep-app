// Activity events: what a request did, as counts, and the fixed layout one
// event takes as an analytics data point. No content and no id reaches a
// point: the actor arrives here already hashed.

export const ACTIVITY_APP = 'prep';

/** A request carrying this header as `1` reports nothing: scripted checks
 * send it so they are not counted as use. Not a secret. */
export const ACTIVITY_SKIP_HEADER = 'x-activity-skip';

export type ActivityAction = 'active' | 'signup' | 'card_review' | 'deck_created';

export interface ActivityEvent {
  action: ActivityAction;
  count: number;
}

export interface ActivityDataPoint {
  indexes: [string];
  blobs: [string, string, string];
  doubles: [number];
}

/** What the hash covers: the app prefix keeps one id from matching across apps. */
export const actorMessage = (actorId: string): string => `${ACTIVITY_APP}:${actorId}`;

export function activityEvents(seen: { active: boolean; signup: boolean; reviews: number; decks: number }): ActivityEvent[] {
  const events: ActivityEvent[] = [];
  if (seen.active) events.push({ action: 'active', count: 1 });
  if (seen.signup) events.push({ action: 'signup', count: 1 });
  if (seen.reviews > 0) events.push({ action: 'card_review', count: seen.reviews });
  if (seen.decks > 0) events.push({ action: 'deck_created', count: seen.decks });
  return events;
}

export function dataPoint(actorHash: string, event: ActivityEvent): ActivityDataPoint {
  return { indexes: [actorHash], blobs: [ACTIVITY_APP, event.action, ''], doubles: [event.count] };
}
