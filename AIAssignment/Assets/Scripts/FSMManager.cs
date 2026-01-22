using UnityEngine;

public class FSMManager : MonoBehaviour
{
    private string currentState = "Patrol";
    private Transform targetEnemy;
    public string CurrentState => currentState;
    
    private GuardController controller;
    private PatrolPoints patrolPoints;
    private int currentPatrolIndex = 0;
    private float searchDuration = 0f;
    private const float SEARCH_TIMEOUT = 5f;
    
    private void Start()
    {
        controller = GetComponent<GuardController>();
        patrolPoints = GetComponent<PatrolPoints>();
    }
    
    public void Initialize() { }
    
    public void Update()
    {
        switch (currentState)
        {
            case "Patrol":
                UpdatePatrol();
                break;
            case "Chase":
                UpdateChase();
                break;
            case "Search":
                UpdateSearch();
                break;
        }
    }
    
    private void UpdatePatrol()
    {
        // Check for enemies in vision
        if (DetectEnemy(out Transform enemy))
        {
            targetEnemy = enemy;
            currentState = "Chase";
            return;
        }
        
        // Patrol behavior
        Vector3 targetPoint = patrolPoints.GetPatrolPoint(currentPatrolIndex);
        controller.MoveTowards(targetPoint);
        
        if (controller.HasReachedDestination())
        {
            currentPatrolIndex = (currentPatrolIndex + 1) % patrolPoints.PointCount;
        }
    }
    
    private void UpdateChase()
    {
        if (targetEnemy == null || !CanSeeTarget(targetEnemy))
        {
            currentState = "Search";
            searchDuration = 0f;
            return;
        }
        
        controller.MoveTowards(targetEnemy.position);
    }
    
    private void UpdateSearch()
    {
        searchDuration += Time.deltaTime;
        
        if (searchDuration > SEARCH_TIMEOUT)
        {
            currentState = "Patrol";
            targetEnemy = null;
            return;
        }
        
        if (DetectEnemy(out Transform enemy))
        {
            targetEnemy = enemy;
            currentState = "Chase";
        }
    }
    
    private bool DetectEnemy(out Transform enemy)
    {
        Collider[] colliders = Physics.OverlapSphere(transform.position, controller.detectionRange);
        
        foreach (Collider col in colliders)
        {
            if ((col.CompareTag("Strategist") || col.CompareTag("Gladiator")) && 
                CanSeeTarget(col.transform))
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
}
