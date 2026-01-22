using UnityEngine;
using UnityEngine.AI;
using System.Collections.Generic;

public class StrategistController : MonoBehaviour
{
    [HideInInspector] public float health = 100f;
    [HideInInspector] public float ammo = 100f;
    
    private NavMeshAgent agent;
    private List<Action> actions;
    private Action currentAction;
    
    public float detectionRange = 20f;
    public float attackRange = 10f;
    
    private Transform targetEnemy;
    private Transform targetResource;
    
    private float decisionTimer = 0.5f;
    private const float DECISION_INTERVAL = 0.5f;
    
    private void Start()
    {
        agent = GetComponent<NavMeshAgent>();
        InitializeActions();
    }
    
    private void InitializeActions()
    {
        actions = new List<Action>
        {
            new FleeAction(),
            new HealAction(),
            new CollectAmmoAction(),
            new AttackAction()
        };
    }
    
    private void Update()
    {
        decisionTimer -= Time.deltaTime;
        
        if (decisionTimer <= 0f)
        {
            MakeDecision();
            decisionTimer = DECISION_INTERVAL;
        }
        
        if (currentAction != null)
        {
            currentAction.Execute(this);
        }
        
        UpdateHealth();
    }
    
    private void MakeDecision()
    {
        float highestUtility = -1f;
        Action bestAction = null;
        
        foreach (Action action in actions)
        {
            float utility = action.CalculateUtility(this);
            
            if (utility > highestUtility)
            {
                highestUtility = utility;
                bestAction = action;
            }
        }
        
        if (bestAction != null)
        {
            currentAction = bestAction;
            Debug.Log($"Strategist choosing: {bestAction.name} (utility: {highestUtility})");
        }
    }
    
    public void SeekNearestResource(string resourceType)
    {
        GameObject[] resources = GameObject.FindGameObjectsWithTag(resourceType);
        
        if (resources.Length == 0) return;
        
        Transform nearest = resources[0].transform;
        float nearestDistance = Vector3.Distance(transform.position, nearest.position);
        
        foreach (GameObject resource in resources)
        {
            float distance = Vector3.Distance(transform.position, resource.transform.position);
            if (distance < nearestDistance)
            {
                nearest = resource.transform;
                nearestDistance = distance;
            }
        }
        
        agent.SetDestination(nearest.position);
        targetResource = nearest;
    }
    
    public bool HasDetectedEnemy()
    {
        Collider[] colliders = Physics.OverlapSphere(transform.position, detectionRange);
        
        foreach (Collider col in colliders)
        {
            if (col.CompareTag("Guard") || col.CompareTag("Gladiator"))
            {
                targetEnemy = col.transform;
                return true;
            }
        }
        
        return false;
    }
    
    public float DistanceToNearestEnemy()
    {
        if (targetEnemy != null)
            return Vector3.Distance(transform.position, targetEnemy.position);
        
        return float.MaxValue;
    }
    
    public void AttackNearestEnemy()
    {
        if (targetEnemy == null) return;
        
        agent.SetDestination(targetEnemy.position);
        
        float distance = Vector3.Distance(transform.position, targetEnemy.position);
        if (distance < attackRange && ammo > 0)
        {
            // Fire weapon
            ammo -= Time.deltaTime * 20f;
        }
    }
    
    public void FleeFromNearestEnemy()
    {
        if (targetEnemy == null) return;
        
        Vector3 fleeDirection = (transform.position - targetEnemy.position).normalized;
        Vector3 fleeTarget = transform.position + fleeDirection * 15f;
        
        agent.SetDestination(fleeTarget);
    }
    
    private void UpdateHealth()
    {
        // Slowly regenerate health when safe
        if (health < 100f && !HasDetectedEnemy())
        {
            health += Time.deltaTime * 5f;
        }
    }
    
    private void OnTriggerEnter(Collider col)
    {
        if (col.CompareTag("HealthPack"))
        {
            health = 100f;
            Destroy(col.gameObject);
        }
        
        if (col.CompareTag("AmmoCrate"))
        {
            ammo = 100f;
            Destroy(col.gameObject);
        }
    }
}
