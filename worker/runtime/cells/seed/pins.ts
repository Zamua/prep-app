// Timestamps the seed profiles fix relative to the clock, which no
// repository method exposes. Test seeding only: no production path holds one.

export interface TestPins {
  session(sid: string, lastActive: string, createdAt?: string | null): void;
  answerInSession(sid: string, qid: number, answeredAt: string, result: string): void;
  pinnedAt(deckId: number, pinnedAt: string): void;
  notificationSentAt(noteId: number, sentAt: string): void;
  workflowStartedAt(workflowId: string, startedAt: string): void;
  tokenCreatedAt(tokenId: number, createdAt: string): void;
}
