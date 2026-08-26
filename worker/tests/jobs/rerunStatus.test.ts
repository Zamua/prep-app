import { describe, expect, it } from 'vitest';
import { PLAN_GRAPH } from '../../app/jobs/graph';

describe('a re-run keeps the gate transient', () => {
  it('the plan node re-entered by feedback reports replanning, not planning', () => {
    const gate = PLAN_GRAPH.nodes.find((n) => n.kind === 'gate');
    const feedback = gate?.gate?.onEvent['feedback'];
    expect(feedback?.transient).toBe('replanning');
    expect(feedback?.go).toEqual({ rerun: 'plan' });
    // The first round is the node's own status; a later round is the
    // transient the gate wrote, for the whole round.
    expect(PLAN_GRAPH.nodes[0]!.status).toBe('planning');
  });
});
