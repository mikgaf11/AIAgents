using UnityEngine;
using System.Collections.Generic;

public class HierarchicalDecisionMaker : MonoBehaviour
{
    private StrategistController strategist;
    private List<Goal> goals;
    private Goal currentPriorityGoal;
    
    private void Start()
    {
        strategist = GetComponent<StrategistController>();
        InitializeGoals();
    }
    
    private void InitializeGoals()
    {
        goals = new List<Goal>
        {
            new SurvivalGoal(),
            new ResourceGatheringGoal(),
            new DominationGoal()
        };
        
        goals.Sort((a, b) => b.priority.CompareTo(a.priority));
    }
    
    public Goal GetCurrentPriorityGoal()
    {
        // Override normal utility AI if critical goal is unmet
        foreach (Goal goal in goals)
        {
            if (goal.priority >= 100 && !goal.IsGoalMet(strategist))
            {
                currentPriorityGoal = goal;
                return goal;
            }
        }
        
        currentPriorityGoal = null;
        return null;
    }
}
