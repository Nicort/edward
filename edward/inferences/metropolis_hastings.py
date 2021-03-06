from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import six
import tensorflow as tf

from edward.inferences.monte_carlo import MonteCarlo
from edward.models import RandomVariable, Uniform
from edward.util import copy


class MetropolisHastings(MonteCarlo):
  """Metropolis-Hastings.

  Notes
  -----
  In conditional inference, we infer z in p(z, \beta | x) while fixing
  inference over \beta using another distribution q(\beta).
  To calculate the acceptance ratio, MetropolisHastings uses an
  estimate of the marginal density,

  .. math::

    p(x, z) = E_{q(\beta)} [ p(x, z, \beta) ]
            \approx p(x, z, \beta^*)

  leveraging a single Monte Carlo sample, where \beta^* ~
  q(\beta). This is unbiased (and therefore asymptotically exact as a
  pseudo-marginal method) if q(\beta) = p(\beta | x).
  """
  def __init__(self, latent_vars, proposal_vars, data=None, model_wrapper=None):
    """
    Parameters
    ----------
    proposal_vars : dict of RandomVariable to RandomVariable
      Collection of random variables to perform inference on; each is
      binded to a proposal distribution p(z' | z).

    Examples
    --------
    >>> z = Normal(mu=0.0, sigma=1.0)
    >>> x = Normal(mu=tf.ones(10) * z, sigma=1.0)
    >>>
    >>> qz = Empirical(tf.Variable(tf.zeros([500])))
    >>> proposal_z = Normal(mu=z, sigma=0.5)
    >>> data = {x: np.array([0.0] * 10, dtype=np.float32)}
    >>> inference = ed.MetropolisHastings({z: qz}, {z: proposal_z}, data)

    Notes
    -----
    The updates assume each Empirical random variable is directly
    parameterized by tf.Variables().
    """
    self.proposal_vars = proposal_vars
    super(MetropolisHastings, self).__init__(latent_vars, data, model_wrapper)

  def build_update(self):
    """
    Draw sample from proposal conditional on last sample. Then accept
    or reject the sample based on the ratio,

    ratio = log p(x, znew) - log p(x, zold) +
            log g(znew | zold) - log g(zold | znew)
    """
    old_sample = {z: tf.gather(qz.params, tf.maximum(self.t - 1, 0))
                  for z, qz in six.iteritems(self.latent_vars)}

    # Form dictionary in order to replace conditioning on prior or
    # observed variable with conditioning on a specific value.
    dict_swap = {}
    for x, qx in six.iteritems(self.data):
      if isinstance(x, RandomVariable):
        if isinstance(qx, RandomVariable):
          qx_copy = copy(qx, scope='conditional')
          dict_swap[x] = qx_copy.value()
        else:
          dict_swap[x] = qx

    dict_swap_old = dict_swap.copy()
    dict_swap_old.update(old_sample)

    # Draw proposed sample and calculate acceptance ratio.
    new_sample = {}
    ratio = 0.0
    for z, proposal_z in six.iteritems(self.proposal_vars):
      # Build proposal g(znew | zold).
      proposal_znew = copy(proposal_z, dict_swap_old, scope='proposal_znew')
      # Sample znew ~ g(znew | zold).
      new_sample[z] = proposal_znew.value()
      # Increment ratio.
      ratio += tf.reduce_sum(proposal_znew.log_prob(new_sample[z]))

    dict_swap_new = dict_swap.copy()
    dict_swap_new.update(new_sample)

    for z, proposal_z in six.iteritems(self.proposal_vars):
      # Build proposal g(zold | znew).
      proposal_zold = copy(proposal_z, dict_swap_new, scope='proposal_zold')
      # Increment ratio.
      ratio -= tf.reduce_sum(proposal_zold.log_prob(dict_swap_old[z]))

    if self.model_wrapper is None:
      for z in six.iterkeys(self.latent_vars):
        # Build priors p(znew) and p(zold).
        znew = copy(z, dict_swap_new, scope='znew')
        zold = copy(z, dict_swap_old, scope='zold')
        # Increment ratio.
        ratio += tf.reduce_sum(znew.log_prob(dict_swap_new[z]))
        ratio -= tf.reduce_sum(zold.log_prob(dict_swap_old[z]))

      for x in six.iterkeys(self.data):
        if isinstance(x, RandomVariable):
          # Build likelihoods p(x | znew) and p(x | zold).
          x_znew = copy(x, dict_swap_new, scope='x_znew')
          x_zold = copy(x, dict_swap_old, scope='x_zold')
          # Increment ratio.
          ratio += tf.reduce_sum(x_znew.log_prob(dict_swap[x]))
          ratio -= tf.reduce_sum(x_zold.log_prob(dict_swap[x]))
    else:
        x = self.data
        ratio += self.model_wrapper.log_prob(x, new_sample)
        ratio -= self.model_wrapper.log_prob(x, old_sample)

    # Accept or reject sample.
    u = Uniform().sample()
    accept = tf.log(u) < ratio
    sample_values = tf.cond(accept, lambda: list(six.itervalues(new_sample)),
                            lambda: list(six.itervalues(old_sample)))
    if not isinstance(sample_values, list):
      # ``tf.cond`` returns tf.Tensor if output is a list of size 1.
      sample_values = [sample_values]

    sample = {z: sample_value for z, sample_value in
              zip(six.iterkeys(new_sample), sample_values)}

    # Update Empirical random variables.
    assign_ops = []
    variables = {x.name: x for x in
                 tf.get_default_graph().get_collection(tf.GraphKeys.VARIABLES)}
    for z, qz in six.iteritems(self.latent_vars):
      variable = variables[qz.params.op.inputs[0].op.inputs[0].name]
      assign_ops.append(tf.scatter_update(variable, self.t, sample[z]))

    # Increment n_accept (if accepted).
    assign_ops.append(self.n_accept.assign_add(tf.select(accept, 1, 0)))
    return tf.group(*assign_ops)
