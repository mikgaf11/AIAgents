using UnityEngine;
using UnityEngine.AI;

public class GuardController : MonoBehaviour
{
    private NavMeshAgent agent;
    private FSMManager fsm;
    public float detectionRange = 15f;
    public float patrolSpeed = 3.5f;
    public float chaseSpeed = 7f;
    
    private void Start()
    {
        agent = GetComponent<NavMeshAgent>();
        fsm = GetComponent<FSMManager>();
        fsm.Initialize();
    }
    
    private void Update()
    {
        fsm.Update();
        HandleMovement();
    }
    
    public void MoveTowards(Vector3 target)
    {
        agent.speed = fsm.CurrentState == "Chase" ? chaseSpeed : patrolSpeed;
        agent.SetDestination(target);
    }
    
    public bool HasReachedDestination()
    {
        return !agent.pathPending && agent.remainingDistance <= agent.stoppingDistance;
    }
    
    private void HandleMovement()
    {
        // Movement handled by NavMeshAgent
    }
}
