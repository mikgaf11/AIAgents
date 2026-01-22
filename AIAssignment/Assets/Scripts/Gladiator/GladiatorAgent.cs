using Unity.MLAgents;
using Unity.MLAgents.Actuators;
using Unity.MLAgents.Sensors;
using UnityEngine;

public class GladiatorAgent : Agent
{
    private Rigidbody rb;
    public float moveSpeed = 10f;
    public float health = 100f;
    public float ammo = 100f;
    
    private float initialHealth = 100f;
    private int opponentCount = 0;
    
    public override void Initialize()
    {
        rb = GetComponent<Rigidbody>();
        initialHealth = health;
    }
    
    public override void OnEpisodeBegin()
    {
        health = initialHealth;
        ammo = 100f;
        transform.position = new Vector3(Random.Range(-20f, 20f), 0.5f, Random.Range(-20f, 20f));
        rb.velocity = Vector3.zero;
    }
    
    public override void CollectObservations(VectorSensor sensor)
    {
        // Own state
        sensor.AddObservation(health / 100f);
        sensor.AddObservation(ammo / 100f);
        sensor.AddObservation(transform.localPosition.normalized);
        
        // Detect nearby agents
        Collider[] colliders = Physics.OverlapSphere(transform.position, 30f);
        
        int agentCount = 0;
        foreach (Collider col in colliders)
        {
            if (col.CompareTag("Gladiator") && col.gameObject != gameObject)
            {
                Vector3 relativePos = col.transform.position - transform.position;
                sensor.AddObservation(relativePos.normalized);
                sensor.AddObservation(Vector3.Distance(transform.position, col.transform.position) / 30f);
                agentCount++;
            }
        }
        
        // Pad observations
        while (agentCount < 2)
        {
            sensor.AddObservation(Vector3.zero);
            sensor.AddObservation(0f);
            agentCount++;
        }
    }
    
    public override void OnActionReceived(ActionBuffers actions)
    {
        // Movement
        float moveX = actions.ContinuousActions[0];
        float moveZ = actions.ContinuousActions[1];
        
        Vector3 moveDirection = new Vector3(moveX, 0f, moveZ).normalized;
        rb.velocity = moveDirection * moveSpeed;
        
        // Attack action
        if (actions.DiscreteActions[0] == 1)
        {
            TryAttack();
        }
        
        // Small penalty for each step
        AddReward(-0.001f);
        
        // Reward for staying alive
        AddReward(health / 1000f);
    }
    
    private void TryAttack()
    {
        if (ammo <= 0) return;
        
        Collider[] colliders = Physics.OverlapSphere(transform.position, 10f);
        
        foreach (Collider col in colliders)
        {
            if (col.CompareTag("Guard") || col.CompareTag("Strategist"))
            {
                col.GetComponent<Agent>()?.TakeDamage(25f);
                AddReward(0.1f);
                ammo -= 10f;
            }
        }
    }
    
    public void TakeDamage(float damageAmount)
    {
        health -= damageAmount;
        AddReward(-0.05f);
        
        if (health <= 0f)
        {
            EndEpisode();
        }
    }
    
    private void OnCollisionEnter(Collision collision)
    {
        if (collision.gameObject.CompareTag("HealthPack"))
        {
            health = 100f;
            AddReward(0.2f);
            Destroy(collision.gameObject);
        }
        
        if (collision.gameObject.CompareTag("AmmoCrate"))
        {
            ammo = 100f;
            AddReward(0.1f);
            Destroy(collision.gameObject);
        }
    }
}
