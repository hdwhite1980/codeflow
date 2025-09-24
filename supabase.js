import { createClient } from '@supabase/supabase-js'

// These environment variables should be set in your .env.local file
const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL
const supabaseKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY

if (!supabaseUrl || !supabaseKey) {
  throw new Error('Missing Supabase environment variables. Please check your .env.local file.')
}

export const supabase = createClient(supabaseUrl, supabaseKey)

// Example usage functions (uncomment and modify as needed):

/*
// Example: Insert data
export async function insertData(table, data) {
  const { data: result, error } = await supabase
    .from(table)
    .insert([data])
  
  if (error) throw error
  return result
}

// Example: Fetch data
export async function fetchData(table) {
  const { data, error } = await supabase
    .from(table)
    .select('*')
  
  if (error) throw error
  return data
}

// Example: Update data
export async function updateData(table, id, updates) {
  const { data, error } = await supabase
    .from(table)
    .update(updates)
    .eq('id', id)
  
  if (error) throw error
  return data
}

// Example: Delete data
export async function deleteData(table, id) {
  const { data, error } = await supabase
    .from(table)
    .delete()
    .eq('id', id)
  
  if (error) throw error
  return data
}
*/