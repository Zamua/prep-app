package workflows

import (
	"errors"
	"testing"

	"github.com/stretchr/testify/mock"
	"github.com/stretchr/testify/require"
	"go.temporal.io/sdk/testsuite"

	"prep-worker/activities"
	"prep-worker/shared"
)

// Failure path: the final progress carries the activity error so the
// grading page can render it instead of a generic failure hint.
func TestGradeAnswerFailureExposesErrorInProgress(t *testing.T) {
	var ts testsuite.WorkflowTestSuite
	env := ts.NewTestWorkflowEnvironment()

	var a *activities.Activities
	env.OnActivity(a.GradeFreeText, mock.Anything, mock.Anything).
		Return(shared.Verdict{}, errors.New("free AI is busy right now"))

	env.ExecuteWorkflow(GradeAnswer, shared.GradeAnswerInput{
		QuestionID: 1,
		UserAnswer: "an answer",
		UserID:     "u@example.com",
	})

	require.True(t, env.IsWorkflowCompleted())
	require.Error(t, env.GetWorkflowError())

	v, err := env.QueryWorkflow(QueryGetGradeProgress)
	require.NoError(t, err)
	var p shared.GradeProgress
	require.NoError(t, v.Get(&p))
	require.Equal(t, "failed", p.Status)
	require.Contains(t, p.Error, "free AI is busy right now")
	require.NotEmpty(t, p.FinishedAt)
}

// Happy path keeps Error empty and lands the result.
func TestGradeAnswerSuccessLeavesErrorEmpty(t *testing.T) {
	var ts testsuite.WorkflowTestSuite
	env := ts.NewTestWorkflowEnvironment()

	var a *activities.Activities
	env.OnActivity(a.GradeFreeText, mock.Anything, mock.Anything).
		Return(shared.Verdict{Result: "right", Feedback: "ok"}, nil)
	env.OnActivity(a.RecordReview, mock.Anything, mock.Anything).
		Return(shared.SRSState{Step: 1, NextDue: "soon", IntervalMinutes: 10}, nil)

	env.ExecuteWorkflow(GradeAnswer, shared.GradeAnswerInput{
		QuestionID: 1,
		UserAnswer: "an answer",
		UserID:     "u@example.com",
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())

	v, err := env.QueryWorkflow(QueryGetGradeProgress)
	require.NoError(t, err)
	var p shared.GradeProgress
	require.NoError(t, v.Get(&p))
	require.Equal(t, "done", p.Status)
	require.Empty(t, p.Error)
}
