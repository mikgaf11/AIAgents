using UnityEngine;
using UnityEngine.AI;

public class GladiatorAI : MonoBehaviour
{
    private NavMeshAgent agent;
    public float health = 100f;
    public float ammo = 50f;
    public float patrolSpeed = 3.5f;
    public float chaseSpeed = 7f;
    public float detectionRange = 20f;
    public float attackRange = 10f;
    
    private Transform targetEnemy;
    private Vector3[] patrolPoints;
    private int currentPatrolIndex = 0;
    private string currentState = "Patrol"; // Patrol, Chase, Attack
    
    private void Start()
    {
        agent = GetComponent<NavMeshAgent>();
        SetupPatrolPoints();
    }
    
    private void SetupPatrolPoints()
    {
        patrolPoints = new Vector3[]
        {
            new Vector3(-15f, 0.5f, -15f),
            new Vector3(15f, 0.5f, -15f),
            new Vector3(15f, 0.5f, 15f),
            new Vector3(-15f, 0.5f, 15f)
        };
    }
    
    private void Update()
    {
        // Detect enemies
        if (DetectEnemy(out Transform enemy))
        {
            targetEnemy = enemy;
            currentState = "Chase";
        }
        
        // Update state behavior
        switch (currentState)
        {
            case "Patrol":
                PatrolBehavior();
                break;
            case "Chase":
                ChaseBehavior();
                break;
        }
        
        // Regenerate health slowly
        if (health < 100f)
            health += Time.deltaTime * 2f;
    }
    
    private void PatrolBehavior()
    {
        agent.speed = patrolSpeed;
        agent.SetDestination(patrolPoints[currentPatrolIndex]);
        
        if (!agent.pathPending && agent.remainingDistance < 1f)
        {
            currentPatrolIndex = (currentPatrolIndex + 1) % patrolPoints.Length;
        }
    }
    
    private void ChaseBehavior()
    {
        if (targetEnemy == null || Vector3.Distance(transform.position, targetEnemy.position) > detectionRange * 2)
        {
            currentState = "Patrol";
            targetEnemy = null;
            return;
        }
        
        agent.speed = chaseSpeed;
        agent.SetDestination(targetEnemy.position);
        
        // Attack if close enough
        if (Vector3.Distance(transform.position, targetEnemy.position) < attackRange && ammo > 0)
        {
            ammo -= Time.deltaTime * 10f;
        }
    }
    
    private bool DetectEnemy(out Transform enemy)
    {
        Collider[] colliders = Physics.OverlapSphere(transform.position, detectionRange);
        
        foreach (Collider col in colliders)
        {
            if ((col.CompareTag("Guard") || col.CompareTag("Strategist")) && CanSeeTarget(col.transform))
            {
                enemy = col.transform;
                return true;
            }
        }
        
        enemy = null;
        return false;
    }
    
    private bool CanSeeTarget(Transform target)
    {
        Vector3 directionToTarget = (target.position - transform.position).normalized;
        float distanceToTarget = Vector3.Distance(transform.position, target.position);
        
        if (Physics.Raycast(transform.position, directionToTarget, out RaycastHit hit, distanceToTarget))
        {
            return hit.transform == target;
        }
        
        return false;
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
            ammo = 50f;
            Destroy(col.gameObject);
        }
    }
}
