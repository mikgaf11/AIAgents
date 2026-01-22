using UnityEngine;

public abstract class Action
{
    public string name;
    public virtual float CalculateUtility(StrategistController strategist) => 0f;
    public abstract void Execute(StrategistController strategist);
}

public class HealAction : Action
{
    public HealAction() { name = "Heal"; }
    
    public override float CalculateUtility(StrategistController strategist)
    {
        if (strategist.health >= 80f) return 0f;
        
        // Higher utility when health is low
        return (100f - strategist.health) / 100f;
    }
    
    public override void Execute(StrategistController strategist)
    {
        strategist.SeekNearestResource("HealthPack");
    }
}

public class CollectAmmoAction : Action
{
    public CollectAmmoAction() { name = "CollectAmmo"; }
    
    public override float CalculateUtility(StrategistController strategist)
    {
        if (strategist.ammo >= 80f) return 0f;
        
        return (100f - strategist.ammo) / 100f * 0.7f; // Lower priority than health
    }
    
    public override void Execute(StrategistController strategist)
    {
        strategist.SeekNearestResource("AmmoCrate");
    }
}

public class AttackAction : Action
{
    public AttackAction() { name = "Attack"; }
    
    public override float CalculateUtility(StrategistController strategist)
    {
        if (!strategist.HasDetectedEnemy()) return 0f;
        if (strategist.health < 40f) return 0f; // Don't attack if low health
        
        // Higher utility when health is good and enemy is close
        float healthFactor = strategist.health / 100f;
        float distanceFactor = 1f / (1f + strategist.DistanceToNearestEnemy());
        
        return healthFactor * distanceFactor * 0.8f;
    }
    
    public override void Execute(StrategistController strategist)
    {
        strategist.AttackNearestEnemy();
    }
}

public class FleeAction : Action
{
    public FleeAction() { name = "Flee"; }
    
    public override float CalculateUtility(StrategistController strategist)
    {
        if (strategist.health > 50f) return 0f;
        if (!strategist.HasDetectedEnemy()) return 0f;
        
        // Critical: flee when health is low
        return 1f;
    }
    
    public override void Execute(StrategistController strategist)
    {
        strategist.FleeFromNearestEnemy();
    }
}
