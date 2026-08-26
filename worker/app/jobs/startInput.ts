// What a start puts in a job input. An LLM step runs in the JobCell, which
// holds the agent and no repositories, so everything the model is shown and
// every ceiling the call is held to is read here or nowhere.
import { maxCardsPerCall } from '../agent/funding.js';
import type { PlanGenerateInput, TriviaGenerateInput, UserRepos } from '../ports.js';

export function planStartInput(repos: UserRepos, deckId: number, deckName: string, prompt: string, freeTierConfigured: boolean): PlanGenerateInput {
  return { deckId, deckName, prompt, maxCards: maxCardsPerCall(repos, freeTierConfigured) };
}

export function triviaStartInput(repos: UserRepos, deckId: number, deckName: string, topic: string, freeTierConfigured: boolean): TriviaGenerateInput {
  return {
    deckId,
    deckName,
    topic,
    batchSize: maxCardsPerCall(repos, freeTierConfigured),
    // The dedupe block the Go activity loaded per call; without it every
    // refill asks the model for questions the deck already holds.
    existing: repos.trivia.existingPrompts(deckId),
  };
}
