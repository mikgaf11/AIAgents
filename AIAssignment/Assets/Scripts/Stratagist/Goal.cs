using System.Collections.Generic;

public abstract class Goal
{
    public string name;
    public int priority; // Higher = more important
    
    public abstract bool IsGoalMet(StrategistController strategist);
    public abstract List<Action> GetActionsForGoal(StrategistController strategist);
}

public class SurvivalGoal : Goal
{
    public SurvivalGoal()
    {
        name = "Survival";
        priority = 100; // Highest priority
    }
    
    public override bool IsGoalMet(StrategistController strategist)
    {
        return strategist.health > 70f;
    }
    
    public override List<Action> GetActionsForGoal(StrategistController strategist)
    {
        return new List<Action> { new FleeAction(), new HealAction() };
    }
}

public class ResourceGatheringGoal : Goal
{
    public ResourceGatheringGoal()
    {
        name = "GatherResources";
        priority = 50;
    }
    
    public override bool IsGoalMet(StrategistController strategist)
    {
        return strategist.health > 80f && strategist.ammo > 80f;
    }
    
    public override List<Action> GetActionsForGoal(StrategistController strategist)
    {
        return new List<Action> { new HealAction(), new CollectAmmoAction() };
    }
}

public class DominationGoal : Goal
{
    public DominationGoal()
    {
        name = "Domination";
        priority = 40;
    }
    
    public override bool IsGoalMet(StrategistController strategist)
    {
        return false; // Never fully met
    }
    
    public override List<Action> GetActionsForGoal(StrategistController strategist)
    {
        return new List<Action> { new AttackAction() };
    }
}
